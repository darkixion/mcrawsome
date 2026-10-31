import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from test_markers import unpack
from test_timeline_archive import documents

from mcrawsome.io.clip import Clip, RawFrame
from mcrawsome.io.container import ContainerWriter, read_frame_record, read_index
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence
from mcrawsome.recipe.raw import RawCalibration
from mcrawsome.resolve import timeline_archive
from mcrawsome.resolve.bin_archive import Media
from mcrawsome.resolve.motion_markers import prepare


@pytest.fixture
def recording(make_mcraw: Callable[..., Path], tmp_path: Path) -> tuple[MountedSequence, Media]:
    original = make_mcraw(tmp_path / "original.mcraw", [i * 41_666_667 for i in range(72) if i != 36])
    source = tmp_path / "synthetic.mcraw"
    with original.open("rb") as stream:
        index = read_index(stream)
        received = None
        with ContainerWriter(source, index.metadata) as writer:
            for ref in index.frames:
                payload, raw = read_frame_record(stream, ref.offset)
                metadata = json.loads(raw)
                if received is None:
                    received = int(metadata["recvdTimestampMs"])
                metadata["recvdTimestampMs"] = received + round(ref.timestamp_ns / 1e6)
                writer.add_frame(ref.timestamp_ns, payload, json.dumps(metadata).encode())
    source.with_suffix(".mcrawsome.toml").write_text("[motion]\nstabilise='off'\nmarkers=true\n")
    source.with_suffix(".gcsv").write_text(
        f"GYROFLOW IMU LOG\ntimestamp,{received}\ntscale,0.005\ngscale,1\nascale,1\norientation,XYZ\n"
        "t,gx,gy,gz,ax,ay,az\n" + "".join(f"{i},0,0,0,0,-9.80665,0\n" for i in range(801))
    )
    with Sequence(source, frame_range=(12, 48)) as seq:
        assert len(set(seq.sources)) < len(seq.sources)
        mount = MountedSequence(
            id="markers",
            source=source,
            path=tmp_path / "virtual",
            stamp=seq.stamp,
            frames=48,
            frame_range=(12, 48),
            active=True,
            serving=True,
        )
        media = Media(mount.path, source.stem, 48, float(seq.rate), seq.frame_info(0), 75000, seq.stamp[3])
    return mount, media


def test_native_send_keeps_trim_and_filled_frames_in_marker_time(
    recording: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recording
    monkeypatch.setattr(timeline_archive, "mounted_media", lambda _: media)
    real = Clip.raw_frame
    sampled = []

    def clipped(clip: Clip, index: int) -> RawFrame:
        sampled.append(index)
        raw = real(clip, index)
        return replace(raw, data=np.full_like(raw.data, raw.metadata.white_level))

    monkeypatch.setattr(Clip, "raw_frame", clipped)
    path = tmp_path / "motion.drt"
    told: list[tuple[str | None, str]] = []
    stabilised = timeline_archive.write_timeline(
        path,
        [mount],
        name="Motion",
        stabilise=True,
        progress=lambda clip, text: told.append((clip.id if clip is not None else None, text)),
    )
    assert stabilised == 0
    assert told == [("markers", "Finding review markers"), (None, "Writing the timeline")]
    node = documents(path)[0].find(".//Sm2SequenceLockableBlob")
    assert node is not None
    markers = unpack(bytes.fromhex(node.findtext("FieldsBlob") or ""))
    clipping = sorted(row[0] for row in markers if row[4] == "Highlight clipping")
    assert clipping[0] == 0
    assert clipping[-1] == 47
    assert len(sampled) <= 4
    assert any(row[0] == 25 and row[4] == "Recording gap" for row in markers)


def test_marker_analysis_does_not_start_picture_normalisation_or_crop(
    recording: tuple[MountedSequence, Media], monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recording
    mount = mount.model_copy(
        update={"raw_settings": RawCalibration(mount_normalise_exposure="picture", mount_crop="auto")}
    )
    markers, check = prepare(mount, media)
    assert all(marker.name not in {"Low motion", "Steady rotation"} for marker in markers)
    mount.source.with_suffix(".gcsv").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        check()


def test_bad_sensor_log_skips_sensor_flags_and_keeps_other_review_checks(
    recording: tuple[MountedSequence, Media], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recording
    monkeypatch.setattr(timeline_archive, "mounted_media", lambda _: media)
    mount.source.with_suffix(".gcsv").write_text("not a gyro log")
    path = tmp_path / "bad.drt"
    assert timeline_archive.write_timeline(path, [mount], name="Bad") == 0
    assert path.exists()


def test_remaining_tilt_uses_the_actual_correction_plan(
    recording: tuple[MountedSequence, Media], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from scipy.spatial.transform import Rotation

    from mcrawsome.camera.profile import Axes
    from mcrawsome.motion.plan import StabilisationPlan

    mount, media = recording
    with Clip(mount.source) as clip:
        count = clip.frame_count
        recorded_turn = 180 if clip.frame_metadata(0).orientation == 3 else 0
    angle = np.radians(recorded_turn + 12)
    camera_up = [-np.sin(angle), -np.cos(angle), 0]
    monkeypatch.setattr(
        "mcrawsome.resolve.motion_markers.reference_up",
        lambda log, orientation, axes, centres, check: np.tile(camera_up, (len(centres), 1)),
    )
    monkeypatch.setattr(
        "mcrawsome.resolve.motion_markers.camera_profile",
        lambda *args: SimpleNamespace(
            gcsv_sync_ms=0,
            readout_ms=0,
            focal_px=100.0,
            gcsv_gravity_axes=Axes(axes=(0, 1, 2), signs=(1, 1, 1)),
        ),
    )
    correction = Rotation.from_euler("z", 12, degrees=True).as_matrix().astype(np.float32)
    plan = StabilisationPlan(
        np.tile(correction, (count, 1, 1, 1)),
        np.tile([1.0, 0, 0, 0], (count, 1)),
        100,
        1,
        128,
        64,
        0,
        12,
        12,
        1,
    )
    uncorrected, _ = prepare(mount, media, display_rotation=90)
    assert any(marker.name == "Recorded tilt" for marker in uncorrected)
    assert not any(marker.name == "Remaining tilt" for marker in uncorrected)
    markers, _ = prepare(mount, media, plan=plan)
    assert any(marker.name == "Recorded tilt" for marker in markers)
    assert not any(marker.name == "Remaining tilt" for marker in markers)
    rotated, _ = prepare(mount, media, plan=plan, display_rotation=90)
    assert any(marker.name == "Remaining tilt" for marker in rotated)


def test_review_markers_work_without_a_gyro_log_and_decode_at_most_120_frames(
    recording: tuple[MountedSequence, Media], monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recording
    mount.source.with_suffix(".gcsv").unlink()
    decoded: list[int] = []
    real = Clip.raw_frame

    def read(clip: Clip, index: int) -> RawFrame:
        decoded.append(index)
        return real(clip, index)

    monkeypatch.setattr(Clip, "raw_frame", read)
    markers, unchanged = prepare(mount, media)
    unchanged()
    assert 0 < len(decoded) <= 120
    assert len(decoded) == len(set(decoded))
    assert all(marker.name not in {"Recorded tilt", "Remaining tilt", "Possible shake"} for marker in markers)


def test_pixel_and_gap_review_categories_use_their_own_colours(
    recording: tuple[MountedSequence, Media], monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recording
    original = Clip.raw_frame

    def clipped(clip: Clip, index: int) -> RawFrame:
        raw = original(clip, index)
        return replace(raw, data=np.full_like(raw.data, raw.metadata.white_level))

    monkeypatch.setattr(Clip, "raw_frame", clipped)
    markers, _ = prepare(mount, media)
    categories = {marker.name: marker.colour for marker in markers}
    assert categories["Highlight clipping"] == "Yellow"
    assert categories["Recording gap"] == "Sand"

    def dark(clip: Clip, index: int) -> RawFrame:
        raw = original(clip, index)
        black = np.array(raw.metadata.black_level, dtype=np.uint16).reshape(2, 2)
        return replace(raw, data=np.tile(black, (raw.data.shape[0] // 2, raw.data.shape[1] // 2)))

    monkeypatch.setattr(Clip, "raw_frame", dark)
    markers, _ = prepare(mount, media)
    assert any(marker.name == "Low recorded signal" and marker.colour == "Blue" for marker in markers)


def test_disabled_exposure_categories_skip_raw_decode_but_keep_recording_gaps(
    recording: tuple[MountedSequence, Media], monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recording
    mount.source.with_suffix(".mcrawsome.toml").write_text(
        "[motion]\nmarkers=true\n[motion.review_categories]\nclipping=false\nlow_signal=false\n"
    )
    monkeypatch.setattr(
        Clip, "raw_frame", lambda *_: pytest.fail("Disabled exposure analysis must not decode RAW")
    )
    markers, check = prepare(mount, media)
    check()
    assert any(marker.name == "Recording gap" for marker in markers)
    assert not any(marker.name in {"Highlight clipping", "Low recorded signal"} for marker in markers)


def test_disabled_gap_category_is_not_emitted(recording: tuple[MountedSequence, Media]) -> None:
    mount, media = recording
    mount.source.with_suffix(".mcrawsome.toml").write_text(
        "[motion]\nmarkers=true\n[motion.review_categories]\ngaps=false\n"
    )
    markers, check = prepare(mount, media)
    check()
    assert not any(marker.name == "Recording gap" for marker in markers)


def test_sustained_shake_blur_is_its_own_green_category(
    recording: tuple[MountedSequence, Media], monkeypatch: pytest.MonkeyPatch
) -> None:
    mount, media = recording
    log = mount.source.with_suffix(".gcsv")
    header, _ = log.read_text().split("t,gx,gy,gz,ax,ay,az\n")
    # An 8 Hz square wave of 60 rad/s: tens of pixels of blur in every exposure, even on a 128-pixel frame.
    rows = "".join(
        f"{i},{60 * np.sign(np.sin(2 * np.pi * 8 * i * 0.005 + 0.1)):.6f},0,0,0,-9.80665,0\n"
        for i in range(801)
    )
    log.write_text(header + "t,gx,gy,gz,ax,ay,az\n" + rows)
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("blur markers must not decode RAW"))
    mount.source.with_suffix(".mcrawsome.toml").write_text(
        "[motion]\nstabilise='off'\nmarkers=true\n[motion.review_categories]\nclipping=false\nlow_signal=false\n"
    )
    markers, _ = prepare(mount, media)
    blurred = [marker for marker in markers if marker.name == "Motion blur"]
    assert blurred
    assert {marker.colour for marker in blurred} == {"Green"}
    assert "cannot remove the blur" in blurred[0].note
    mount.source.with_suffix(".mcrawsome.toml").write_text(
        "[motion]\nstabilise='off'\nmarkers=true\n[motion.review_categories]\nclipping=false\nlow_signal=false\n"
        "blur=false\n"
    )
    assert not any(marker.name == "Motion blur" for marker in prepare(mount, media)[0])
