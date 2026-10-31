"""Integrated timeline preparation, provenance and failure atomicity without Resolve or FUSE."""

import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import numpy as np
import pytest
from test_stabilisation import blob

from mcrawsome.camera.profile import CameraProfile
from mcrawsome.io.clip import Clip
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.orientation import AxisMapping
from mcrawsome.motion.sync import SyncResult
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence
from mcrawsome.pipeline.stabilise import GyroFit, MotionOutcome
from mcrawsome.recipe.schema import Recipe
from mcrawsome.resolve import gyro, timeline_archive
from mcrawsome.resolve.bin_archive import Media
from mcrawsome.resolve.bin_codec import decode_wire, unwrap
from mcrawsome.resolve.stabilisation import section


@pytest.fixture
def mounted(tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MountedSequence, Media]:
    clip = tiny_raw
    with Sequence(clip) as seq:
        info = seq.frame_info(0)
        mount = MountedSequence(
            id="test",
            source=clip,
            path=tmp_path / "virtual",
            stamp=seq.stamp,
            frames=len(seq.sources),
            active=True,
            serving=True,
        )
        media = Media(mount.path, clip.stem, mount.frames, float(seq.rate), info, 50000, mount.stamp[3])
    monkeypatch.setattr(timeline_archive, "mounted_media", lambda _: media)
    clip.with_suffix(".gcsv").write_bytes(b"original gyro log\r\n")
    clip.with_suffix(".mcrawsome.toml").write_text("[motion]\nbands = 1\n")  # frame-global unless a test asks
    return mount, media


def stub_planner(
    monkeypatch: pytest.MonkeyPatch,
    mount: MountedSequence,
    media: Media,
) -> list[Recipe]:
    from mcrawsome.motion.plan import StabilisationPlan

    seen = []

    def planner(source: Path, recipe: Recipe, workers: int, folder: Path, **kw: Any) -> MotionOutcome:
        assert kw["frame_global"] is True
        assert kw["allow_unverified_sync"] is True  # a gyro send always sends
        kw["check"]()
        kw["stage"]("Reading its gyro")
        seen.append(recipe)
        with Clip(source) as clip:
            count = clip.frame_count
            recorded_flip = clip.frame_metadata(0).orientation == 3
        p = StabilisationPlan(
            np.tile(np.eye(3, dtype=np.float32), (count, 1, 1, 1)),
            np.tile([1.0, 0, 0, 0], (count, 1)),
            100.0,
            recipe.motion.crop,
            media.info.width,
            media.info.height,
            0,
            0,
            0,
            1.0,
            recorded_flip,
        )
        path = folder / "stabilisation.npz"
        p.save(path)
        fit = SyncResult(
            -0.1, AxisMapping((0, 1, 2), (1, -1, 1)), 100.0, 0.9, (0.9, 0.9, 0.9), 2.0, True, None
        )
        log = GyroLog(np.array([0.0, 1.0]), np.zeros((2, 3)), None, None, "XYZ")
        return MotionOutcome(
            path,
            [],
            {"stabilised": True},
            GyroFit(
                fit,
                log,
                CameraProfile(name="test", readout_ms=10.0),
                0.12,
                (media.info.width, media.info.height),
                24.0,
            ),
        )

    monkeypatch.setattr(gyro, "plan_raw_clip", planner)
    return seen


def test_quarter_turn_has_a_clear_error_before_gyro_analysis(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = mounted
    monkeypatch.setattr(
        gyro, "plan_raw_clip", lambda *_a, **_kw: pytest.fail("quarter turn started analysis")
    )
    turned = replace(media, info=replace(media.info, orientation=6))
    with pytest.raises(ValueError, match="recorded-orientation"):
        gyro.prepare(mount, turned, tmp_path / "unsupported")


def test_integrated_gyro_timeline_keeps_original_log_plan_and_effective_settings(
    mounted: tuple[MountedSequence, Media],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount, media = mounted
    mount.source.with_suffix(".mcrawsome.toml").write_text(
        "[motion]\nstrength=0.5\nsmoothing_seconds=0.25\ncrop=1.15\nbands=1\n"
    )
    seen = stub_planner(monkeypatch, mount, media)
    path = tmp_path / "native.drt"
    # Native export writes no image data, including when stabilised.
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("archive serialiser decoded RAW"))
    timeline_archive.write_timeline(
        path,
        [mount],
        name="Gyro",
        stabilise=True,
        rotations=[90],
    )
    assert seen[0].motion.bands == 1
    assert seen[0].motion.stabilise == "on"
    assert seen[0].motion.strength == 0.5
    folder = tmp_path / "native.drt.gyro/0"
    assert (folder / "source.gcsv").read_bytes() == mount.source.with_suffix(".gcsv").read_bytes()
    document = json.loads((folder / "settings.json").read_text())
    assert document["source_frames"] == list(range(media.frames))
    assert document["requested_motion"]["bands"] == 1  # rolling-shutter compensation off for this clip
    assert document["effective_motion"]["bands"] == 1
    assert document["effective_motion"]["smoothing_seconds"] == 0.25
    assert document["frame_global"]
    assert document["row_readout_for_timing_only"]
    assert document["timeline_display_rotation_clockwise"] == 90
    assert document["sync_confirmed"] is True
    assert document["schema"] == 2
    assert document["native_mode"] == "Camera Gyro"
    assert document["native_projection"] == {
        "width": media.info.width,
        "height": media.info.height,
        "focal_px": 100.0,
        "zoom": 1.15,
    }
    assert (folder / "stabilisation.npz").is_file()
    with ZipFile(path) as archive:
        assert len(archive.namelist()) == 3
        assert archive.testzip() is None
        container = next(archive.read(n) for n in archive.namelist() if n.startswith("SeqContainer/"))
        assert b"pLmVerTable" in container
        assert b"EffectFiltersBA" in container
        tree = ET.fromstring(container.replace(b"ListMgt::", b"ListMgt__"))
        body = tree.findtext(".//Sm2TiVideoClip//Body")
        assert body is not None
        raw = unwrap(bytes.fromhex(body))[0]
        for number in (3, 9, 1, 6, 2):
            raw = section(raw, number)
        globals_ = [blob(f.value) for f in decode_wire(raw) if f.number == 3]
        settings = next(
            g for g in globals_ if next(f.value for f in decode_wire(g) if f.number == 1) == 0x10300018
        )
        assert b" 0 3 0 " in section(section(settings, 2), 5)
        frames = decode_wire(section(raw, 4))
        assert len(frames) == media.frames
        first = [blob(f.value) for f in decode_wire(blob(frames[0].value)) if f.number == 2]
        ids = {next(f.value for f in decode_wire(e) if f.number == 1) for e in first}
        assert ids == {0x10300017, 0x1030001E, 0x1030001F}
    assert not list(tmp_path.glob(".mcrawsome-gyro-*"))


@pytest.mark.parametrize("failure", ["log", "source", "cancel"])
def test_failure_after_preparation_publishes_neither_archive_nor_provenance(
    mounted: tuple[MountedSequence, Media],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    mount, media = mounted
    stub_planner(monkeypatch, mount, media)
    original = timeline_archive._write

    def changed(*args: Any, **kwargs: Any) -> None:
        assert (tmp_path / "native.drt.gyro/0/source.gcsv").exists()
        if failure == "log":
            mount.source.with_suffix(".gcsv").write_text("changed gyro")
        elif failure == "source":
            mount.source.write_bytes(b"changed recording")
        else:
            raise InterruptedError("cancelled")
        original(*args, **kwargs)

    monkeypatch.setattr(timeline_archive, "_write", changed)
    path = tmp_path / "native.drt"
    with pytest.raises((ValueError, InterruptedError), match=r"changed|cancelled"):
        timeline_archive.write_timeline(path, [mount], name="Gyro", stabilise=True)
    assert not path.exists()
    assert not (tmp_path / "native.drt.gyro").exists()
    assert not list(tmp_path.glob(".mcrawsome-gyro-*"))
    assert not list(tmp_path.glob("*.part"))


def test_existing_provenance_is_not_replaced(
    mounted: tuple[MountedSequence, Media],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount, media = mounted
    stub_planner(monkeypatch, mount, media)
    folder = tmp_path / "native.drt.gyro"
    folder.mkdir()
    (folder / "keep").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        timeline_archive.write_timeline(tmp_path / "native.drt", [mount], name="Gyro", stabilise=True)
    assert (folder / "keep").read_bytes() == b"keep"
    assert not (tmp_path / "native.drt").exists()


def test_missing_gyro_and_geometry_mismatch_fail_before_publication(
    mounted: tuple[MountedSequence, Media],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount, media = mounted
    mount.source.with_suffix(".gcsv").unlink()
    mount.source.with_suffix(".mcrawsome.toml").write_text('[motion]\nstabilise = "on"\nbands = 1\n')
    with pytest.raises(ValueError, match="gyro log"):
        timeline_archive.write_timeline(tmp_path / "missing.drt", [mount], name="Gyro", stabilise=True)
    mount.source.with_suffix(".gcsv").write_text("gyro")
    bad = replace(media, info=replace(media.info, width=media.info.width + 2))
    with pytest.raises(ValueError, match="geometry"):
        gyro.prepare(mount, bad, tmp_path / "mismatch")
    assert not (tmp_path / "missing.drt").exists()


def test_older_pages_gyro_flags_are_accepted() -> None:
    from mcrawsome.server.routes.mounts import SendMountsRequest

    SendMountsRequest(mounts=["a"], gyro=True, rolling_shutter=True)  # ignored: each clip's settings decide


@pytest.mark.parametrize(
    ("layer", "wanted"),
    [
        ("", True),
        ('[motion]\nstabilise = "off"\n', False),
        ("[motion]\nstrength = 0\n", False),
        ('[motion]\nstabilise = "on"\nstrength = 0\n', True),  # on: always, or a clear failure
    ],
)
def test_a_timeline_stabilises_the_clips_whose_stabilise_switch_is_on(
    mounted: tuple[MountedSequence, Media], layer: str, wanted: bool
) -> None:
    mount, _ = mounted
    mount.source.with_suffix(".mcrawsome.toml").write_text(layer)
    assert gyro.wants_stabilisation(mount.source) is wanted


def test_a_clip_without_a_gyro_log_is_left_as_recorded(mounted: tuple[MountedSequence, Media]) -> None:
    mount, _ = mounted
    mount.source.with_suffix(".gcsv").unlink()
    assert gyro.wants_stabilisation(mount.source) is False


def test_a_timeline_of_clips_with_stabilise_off_is_written_plainly(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, _ = mounted
    mount.source.with_suffix(".mcrawsome.toml").write_text('[motion]\nstabilise = "off"\n')
    monkeypatch.setattr(
        gyro, "plan_raw_clip", lambda *a, **k: pytest.fail("planned a clip with Stabilise off")
    )
    path = tmp_path / "plain.drt"
    assert timeline_archive.write_timeline(path, [mount], name="Plain", stabilise=True) == 0
    assert path.is_file()
    assert not (tmp_path / "plain.drt.gyro").exists()


def test_prepare_refuses_a_clip_with_stabilise_off(
    mounted: tuple[MountedSequence, Media], tmp_path: Path
) -> None:
    mount, media = mounted
    mount.source.with_suffix(".mcrawsome.toml").write_text('[motion]\nstabilise = "off"\n')
    with pytest.raises(ValueError, match="Stabilise is off"):
        gyro.prepare(mount, media, tmp_path / "off")


def test_an_unchanged_clip_sent_again_reuses_its_plan_and_provenance(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = mounted
    seen = stub_planner(monkeypatch, mount, media)
    first = gyro.prepare(mount, media, tmp_path / "first")
    first.corrections.quaternions[:] = 0  # whoever receives a plan may change it
    second = gyro.prepare(mount, media, tmp_path / "second")
    assert len(seen) == 1, "the unchanged clip was analysed again"
    assert second.corrections.quaternions.any()
    third = gyro.prepare(mount, media, tmp_path / "third")
    np.testing.assert_array_equal(second.corrections.quaternions, third.corrections.quaternions)
    for name in ("settings.json", "stabilisation.npz", "source.gcsv"):
        assert (tmp_path / "third" / name).read_bytes() == (tmp_path / "second" / name).read_bytes(), name


@pytest.mark.parametrize("change", ["settings", "gyro log", "camera profile", "display rotation"])
def test_a_clip_whose_plan_inputs_changed_is_analysed_again(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    mount, media = mounted
    seen = stub_planner(monkeypatch, mount, media)
    gyro.prepare(mount, media, tmp_path / "first")
    rotation = 0
    if change == "settings":
        mount.source.with_suffix(".mcrawsome.toml").write_text("[motion]\nbands = 1\nstrength = 0.5\n")
    elif change == "gyro log":
        mount.source.with_suffix(".gcsv").write_bytes(b"another gyro log\r\n")
    elif change == "camera profile":
        from mcrawsome.pipeline.stabilise import camera_profile

        monkeypatch.setattr(
            "mcrawsome.resolve.gyro.camera_profile",
            lambda *a: camera_profile(*a).model_copy(update={"readout_ms": 21.0}),
        )
    else:
        rotation = 180
    gyro.prepare(mount, media, tmp_path / "second", display_rotation=rotation)
    assert len(seen) == 2


def test_a_timeline_tells_each_clips_stages_then_its_own(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = mounted
    stub_planner(monkeypatch, mount, media)
    told: list[tuple[str | None, str]] = []
    timeline_archive.write_timeline(
        tmp_path / "told.drt",
        [mount],
        name="Told",
        stabilise=True,
        progress=lambda clip, text: told.append((clip.id if clip is not None else None, text)),
    )
    assert told == [("test", "Reading its gyro"), ("test", "Stabilised"), (None, "Writing the timeline")]


def test_a_clip_that_fails_stops_the_one_beside_it_and_its_own_error_is_reported(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    mount, _ = mounted
    started = threading.Event()

    def analyse(_mount: MountedSequence, _media: Media, folder: Path, **kw: Any) -> None:
        if folder.name == "0":  # a long analysis that checks for cancellation as it goes
            started.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                kw["check"]()
                time.sleep(0.01)
            pytest.fail("the other clip's failure did not stop this one")
        assert started.wait(5)
        raise ValueError("No MotionCam gyro log beside the second clip")

    monkeypatch.setattr(gyro, "prepare", analyse)
    path = tmp_path / "pair.drt"
    with pytest.raises(ValueError, match="second clip"):
        timeline_archive.write_timeline(path, [mount, mount], name="Pair", stabilise=True)
    assert not path.exists()
    assert not (tmp_path / "pair.drt.gyro").exists()


def test_preparation_uses_the_real_dropped_frame_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_mcraw: Any,
) -> None:
    step = 41_666_667
    source = make_mcraw(tmp_path / "dropped.mcraw", [0, step, 3 * step, 4 * step])
    source.with_suffix(".gcsv").write_text("original gyro log")
    source.with_suffix(".mcrawsome.toml").write_text("[motion]\nbands = 1\n")  # frame-global
    with Sequence(source) as seq:
        mount = MountedSequence(
            id="dropped",
            source=source,
            path=tmp_path / "virtual",
            stamp=seq.stamp,
            frames=len(seq.sources),
            active=True,
            serving=True,
        )
        media = Media(
            mount.path, source.stem, mount.frames, float(seq.rate), seq.frame_info(0), 50000, mount.stamp[3]
        )
        assert seq.sources == [0, 1, 1, 2, 3]
    stub_planner(monkeypatch, mount, media)
    state = gyro.prepare(mount, media, tmp_path / "plan")
    assert len(state.corrections.quaternions) == 5
    np.testing.assert_array_equal(state.corrections.quaternions[1], state.corrections.quaternions[2])
    assert np.all(np.diff(state.corrections.queue_times_ns) > 0)
    document = json.loads((tmp_path / "plan/settings.json").read_text())
    assert document["source_frames"] == [0, 1, 1, 2, 3]
    assert document["source_frame_count"] == 4


def test_motioncam_landscape_two_is_normal_dng_orientation(
    mounted: tuple[MountedSequence, Media],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.io.metadata import FrameMetadata

    mount, media = mounted
    original = Clip.frame_metadata

    def landscape(clip: Clip, index: int) -> FrameMetadata:
        return original(clip, index).model_copy(update={"orientation": 2})

    monkeypatch.setattr(Clip, "frame_metadata", landscape)
    timing = Clip.frame_timing
    monkeypatch.setattr(
        Clip, "frame_timing", lambda clip, index: timing(clip, index).model_copy(update={"orientation": 2})
    )
    with Sequence(mount.source) as seq:
        assert seq.clip.frame_metadata(0).orientation == 2
        assert seq.frame_info(0).orientation == media.info.orientation == 1
    stub_planner(monkeypatch, mount, media)
    path = tmp_path / "landscape.drt"
    timeline_archive.write_timeline(path, [mount], name="Landscape", stabilise=True)
    assert path.is_file()
    document = json.loads((tmp_path / "landscape.drt.gyro/0/settings.json").read_text())
    assert document["geometry"]["orientation"] == 1


def test_a_rolling_shutter_timeline_plans_twelve_row_bands_and_writes_each_to_its_slot(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scipy.spatial.transform import Rotation

    from mcrawsome.motion.plan import StabilisationPlan

    mount, media = mounted
    mount.source.with_suffix(".mcrawsome.toml").write_text("")  # default: rolling-shutter compensation on
    asked: list[int] = []

    def planner(source: Path, recipe: Recipe, workers: int, folder: Path, **kw: Any) -> MotionOutcome:
        asked.append(kw["row_bands"])
        with Clip(source) as clip:
            count = clip.frame_count
            recorded_flip = clip.frame_metadata(0).orientation == 3
        rows = np.stack([Rotation.from_rotvec([0, 0.0005 * band, 0]).as_matrix() for band in range(12)])
        p = StabilisationPlan(
            np.tile(rows.astype(np.float32), (count, 1, 1, 1)), np.tile([1.0, 0, 0, 0], (count, 1)), 100.0,
            recipe.motion.crop, media.info.width, media.info.height, 0, 0, 0, 1.0, recorded_flip,
        )  # fmt: skip
        path = folder / "stabilisation.npz"
        p.save(path)
        fit = SyncResult(
            -0.1, AxisMapping((0, 1, 2), (1, -1, 1)), 100.0, 0.9, (0.9, 0.9, 0.9), 2.0, True, None
        )
        log = GyroLog(np.array([0.0, 1.0]), np.zeros((2, 3)), None, None, "XYZ")
        profile = CameraProfile(name="test", readout_ms=18.0)
        gyro_fit = GyroFit(fit, log, profile, 0.12, (media.info.width, media.info.height), 24.0)
        return MotionOutcome(path, [], {"stabilised": True}, gyro_fit)

    monkeypatch.setattr(gyro, "plan_raw_clip", planner)
    path = tmp_path / "rows.drt"
    timeline_archive.write_timeline(path, [mount], name="Rows", stabilise=True)
    assert asked == [12]
    document = json.loads((tmp_path / "rows.drt.gyro/0/settings.json").read_text())
    assert document["rolling_shutter_rows"] is True
    assert document["frame_global"] is False
    assert document["effective_motion"]["bands"] == 12
    assert "k/11" in document["row_slot_order"]
    with ZipFile(path) as archive:
        container = next(archive.read(n) for n in archive.namelist() if n.startswith("SeqContainer/"))
    tree = ET.fromstring(container.replace(b"ListMgt::", b"ListMgt__"))
    body = tree.findtext(".//Sm2TiVideoClip//Body")
    assert body is not None
    raw = unwrap(bytes.fromhex(body))[0]
    for number in (3, 9, 1, 6, 2):
        raw = section(raw, number)
    first = decode_wire(section(raw, 4))[0]
    fields = {
        next(f.value for f in decode_wire(blob(e.value)) if f.number == 1): section(blob(e.value), 2)
        for e in decode_wire(blob(first.value))
        if e.number == 2
    }
    poses = np.frombuffer(section(fields[0x1030001E], 5), "<f4").reshape(13, 4)
    assert len({tuple(np.round(row, 6)) for row in poses[1:]}) == 12  # every row slot has its own pose


@pytest.fixture
def recorded_mount(
    make_mcraw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[MountedSequence, Media]:
    """A clip whose motion is recorded inside it (MotionCam 5.1), with no .gcsv beside it."""
    from conftest import RECORDED_CONTAINER, RECORDED_FRAME, SYNTHETIC_STEP, recorded_motion

    stamps = [i * SYNTHETIC_STEP for i in range(3)]
    clip = make_mcraw(
        tmp_path / "recorded.mcraw",
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], 0.2),
    )
    with Sequence(clip) as seq:
        info = seq.frame_info(0)
        mount = MountedSequence(
            id="recorded",
            source=clip,
            path=tmp_path / "virtual",
            stamp=seq.stamp,
            frames=len(seq.sources),
            active=True,
            serving=True,
        )
        media = Media(mount.path, clip.stem, mount.frames, float(seq.rate), info, 50000, mount.stamp[3])
    monkeypatch.setattr(timeline_archive, "mounted_media", lambda _: media)
    clip.with_suffix(".mcrawsome.toml").write_text("[motion]\nbands = 1\n")
    return mount, media


def test_a_clip_with_motion_recorded_in_it_is_stabilised_without_a_gyro_log(
    recorded_mount: tuple[MountedSequence, Media],
) -> None:
    mount, _ = recorded_mount
    assert not mount.source.with_suffix(".gcsv").exists()
    assert gyro.wants_stabilisation(mount.source) is True


def test_recorded_motion_is_planned_without_copying_or_hashing_a_log(
    recorded_mount: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recorded_mount
    stub_planner(monkeypatch, mount, media)
    folder = tmp_path / "prepared"
    prepared = gyro.prepare(mount, media, folder)
    assert prepared.log_path == mount.source
    assert prepared.log_sha256 == ""
    prepared.check()  # the recording itself is what must not change
    assert not (folder / "source.gcsv").exists()
    document = json.loads((folder / "settings.json").read_text())
    assert document["gyro_source"] == "recorded in the .mcraw"
    assert "gcsv" not in document
    again = gyro.prepare(mount, media, tmp_path / "again")  # an unchanged clip reuses its plan
    assert again.log_sha256 == ""
    assert not (tmp_path / "again" / "source.gcsv").exists()


def test_correcting_only_the_rolling_shutter_is_sent_at_strength_zero(
    mounted: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = mounted
    mount.source.with_suffix(".mcrawsome.toml").write_text(
        '[motion]\nmode = "rolling_shutter"\nstrength = 0\n'
    )
    assert gyro.wants_stabilisation(mount.source) is True  # strength does not apply to this mode
    asked: list[tuple[Recipe, int]] = []

    def planner(source: Path, recipe: Recipe, workers: int, folder: Path, **kw: Any) -> MotionOutcome:
        asked.append((recipe, kw["row_bands"]))
        raise StabiliseError("planned")  # only what was asked for matters here

    from mcrawsome.pipeline.stabilise import StabiliseError
    from mcrawsome.resolve.camera_gyro import ROWS

    monkeypatch.setattr(gyro, "plan_raw_clip", planner)
    with pytest.raises(ValueError, match="planned"):
        gyro.prepare(mount, media, tmp_path / "rows")
    assert asked[0][0].motion.mode == "rolling_shutter"
    assert asked[0][1] == ROWS  # the rows are planned, though strength is zero
    mount.source.with_suffix(".mcrawsome.toml").write_text('[motion]\nmode = "rolling_shutter"\nbands = 1\n')
    with pytest.raises(ValueError, match="Rolling shutter compensation on"):
        gyro.prepare(mount, media, tmp_path / "no-rows")
