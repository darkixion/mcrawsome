"""The library's scan: clips in the chosen folders, their ids, readiness and facts."""

import json
import os
import shutil
import struct
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.library import scan as scan_module
from mcrawsome.library.scan import Library, clip_id, scan
from mcrawsome.output.sidecar import quick_hash
from mcrawsome.paths import state_dir

STEP = 41_666_667  # ns between frames at 24 fps
MakeClip = Callable[..., Path]


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def _age(*paths: Path, seconds: float = 3600) -> None:
    """Set the files' modification time `seconds` ago: copied long ago, not being written."""
    when = time.time() - seconds
    for path in paths:
        os.utime(path, (when, when))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "clips"
    path.mkdir()
    return path


@pytest.fixture
def clip(make_mcraw: MakeClip) -> MakeClip:
    """A small decodable clip of `frames` frames, settled unless `fresh`; `first` changes its content."""

    def make(path: Path, frames: int = 3, first: int = 0, fresh: bool = False) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        make_mcraw(path, [first + i * STEP for i in range(frames)])
        if not fresh:
            _age(path)
        return path

    return make


def _cut_before_index(path: Path) -> None:
    with path.open("r+b") as file:
        file.seek(-8, 2)
        (offset,) = struct.unpack("<q", file.read(8))
        file.truncate(offset - 8)


def _by_name(clips: list[scan_module.ClipSummary]) -> dict[str, scan_module.ClipSummary]:
    return {clip.name: clip for clip in clips}


@pytest.mark.fixture
def test_scan_lists_the_clips_and_skips_the_rest(
    fixture_10bit_rotated: Path, synthetic_video: Path, folder: Path
) -> None:
    raw = Path(shutil.copy(fixture_10bit_rotated, folder / "a.mcraw"))
    video = Path(shutil.move(synthetic_video, folder / synthetic_video.name))
    for name in (".hidden.mcraw", ".syncthing.b.mcraw.tmp", "a.gcsv", "notes.txt"):
        (folder / name).write_bytes(b"x" * 100)
    _age(*folder.iterdir())
    clips = _by_name(scan([folder]))
    assert sorted(clips) == ["a.mcraw", video.name]
    first = clips["a.mcraw"]
    assert (first.kind, first.status, first.width, first.height, first.fps) == (
        "raw",
        "ready",
        3840,
        2160,
        24.0,
    )
    assert first.duration_s == pytest.approx(3 / 24.0029, abs=1e-3)
    assert first.path == raw
    assert first.paths == [raw]
    assert first.camera == "Google Pixel 10 Pro XL"
    assert first.captured is not None
    assert first.captured.year == 2026
    assert first.id is not None
    assert len(first.id) == 16
    second = clips[video.name]
    assert (second.kind, second.status, second.width, second.height) == ("log", "ready", 320, 180)
    assert second.frame_count == 30
    assert second.fps == 24.0
    assert second.duration_s == pytest.approx(1.25, abs=0.05)


def test_the_id_is_made_from_the_size_and_first_16_mib(folder: Path, clip: MakeClip) -> None:
    import hashlib

    from mcrawsome.output.chunks import InputFile

    path = clip(folder / "a.mcraw")
    given = InputFile.of(path)
    expected = hashlib.sha256(f"{given.size}:{given.sha256_first_16mib}".encode()).hexdigest()[:16]
    assert clip_id(path) == expected
    [listed] = scan([folder])
    assert listed.id == expected


def test_only_one_level_of_subfolders_is_scanned(folder: Path, clip: MakeClip) -> None:
    clip(folder / "a.mcraw")
    clip(folder / "day2" / "b.mcraw", first=1)
    clip(folder / "day2" / "deeper" / "c.mcraw", first=2)
    clip(folder / ".stversions" / "d.mcraw", first=3)
    assert sorted(_by_name(scan([folder]))) == ["a.mcraw", "b.mcraw"]


@pytest.mark.parametrize("editor", ["CacheClip", "ProxyMedia", "Resolve Project Backups"])
def test_davinci_resolves_own_folders_are_not_scanned(folder: Path, clip: MakeClip, editor: str) -> None:
    clip(folder / "Camera" / "a.mcraw")
    clip(folder / editor / "b.mcraw", first=1)
    assert sorted(_by_name(scan([folder]))) == ["a.mcraw"]


def test_a_new_or_growing_file_is_copying_and_gets_no_id(folder: Path, clip: MakeClip) -> None:
    clock = Clock()
    library = Library(clock=clock)
    fresh = clip(folder / "fresh.mcraw", fresh=True)
    grown = clip(folder / "grown.mcraw", first=1)
    clips = _by_name(library.scan([folder]).clips)
    assert (clips["fresh.mcraw"].status, clips["fresh.mcraw"].id) == ("copying", None)
    assert clips["grown.mcraw"].status == "ready"
    clip(grown, frames=4, first=1)  # rewritten, keeping an old modification time (as some sync tools do)
    _age(fresh)
    clock.now += 1
    clips = _by_name(library.scan([folder]).clips)
    assert (clips["grown.mcraw"].status, clips["grown.mcraw"].id) == ("copying", None)
    clock.now += scan_module.SETTLE_S + 1
    clips = _by_name(library.scan([folder]).clips)
    assert clips["grown.mcraw"].status == "ready"
    assert clips["fresh.mcraw"].status == "ready"
    assert clips["fresh.mcraw"].id is not None


def test_a_clip_without_its_index_waits_ten_minutes_then_is_listed_as_recovered(
    folder: Path, clip: MakeClip
) -> None:
    path = clip(folder / "cut.mcraw", frames=4)
    _cut_before_index(path)
    _age(path, seconds=60)
    [listed] = scan([folder])
    assert listed.status == "copying"
    _age(path, seconds=scan_module.RECOVERED_WAIT_S + 60)
    [listed] = scan([folder])
    assert listed.status == "ready"
    assert listed.badges == ["recovered"]
    assert listed.id is not None


def test_a_file_that_does_not_open_is_unreadable_with_the_reason(folder: Path) -> None:
    broken = folder / "broken.mcraw"
    broken.write_bytes(b"not a clip" * 100)
    _age(broken)
    [listed] = scan([folder])
    assert listed.status == "unreadable"
    assert listed.error
    assert listed.id is not None


def test_a_deleted_clip_leaves_and_a_renamed_one_keeps_its_id(folder: Path, clip: MakeClip) -> None:
    library = Library()
    first, second = clip(folder / "a.mcraw"), clip(folder / "b.mcraw", first=1)
    ids = {c.name: c.id for c in library.scan([folder]).clips}
    first.unlink()
    second.rename(folder / "renamed.mcraw")
    clips = _by_name(library.scan([folder]).clips)
    assert sorted(clips) == ["renamed.mcraw"]
    assert clips["renamed.mcraw"].id == ids["b.mcraw"]
    assert library.find(str(ids["a.mcraw"])) is None


def test_a_missing_folder_is_reported_and_the_others_still_listed(
    folder: Path, clip: MakeClip, tmp_path: Path
) -> None:
    clip(folder / "a.mcraw")
    gone = tmp_path / "unplugged"
    view = Library().scan([gone, folder])
    assert [c.name for c in view.clips] == ["a.mcraw"]
    assert view.missing_folders == [gone]


def test_the_same_clip_at_two_paths_has_one_id_and_its_first_path(folder: Path, clip: MakeClip) -> None:
    library = Library()
    original = clip(folder / "b.mcraw")
    copy = folder / "a" / "copy.mcraw"
    copy.parent.mkdir()
    shutil.copy2(original, copy)
    (folder / "b.mcrawsome.toml").write_text('look = "bond"\n')
    [listed] = library.scan([folder]).clips
    assert listed.paths == [copy, original]  # sorted, never the folder's listing order
    assert listed.path == copy
    assert listed.name == "copy.mcraw"
    assert "has-settings" not in listed.badges  # the first path's own Recipe: none
    copy.unlink()
    [listed] = library.scan([folder]).clips
    assert (listed.id, listed.path, listed.paths) == (listed.id, original, [original])
    assert "has-settings" in listed.badges


def test_a_rescan_and_a_restart_read_no_clip_bytes(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip(folder / "a.mcraw")
    clip(folder / "b.mcraw", first=1)
    first = Library().scan([folder])
    assert (state_dir() / "library.json").exists()
    reads: list[Path] = []

    def reading(path: Path) -> str:
        reads.append(path)
        return ""

    monkeypatch.setattr(scan_module, "quick_hash", reading)
    monkeypatch.setattr(scan_module, "Clip", reading)
    library = Library()  # a restart: the ids and facts come from library.json
    assert library.scan([folder]) == first
    assert library.scan([folder]) == first
    assert reads == []


def test_saved_display_turn_refreshes_without_reopening_raw(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = clip(folder / "portrait.mcraw")
    library = Library()
    [original] = library.scan([folder]).clips
    assert original.display_rotation == 0
    source.with_suffix(".mcrawsome.toml").write_text("[develop]\nrotation=90\n")
    assert library.details(original).display_rotation == 90
    monkeypatch.setattr(scan_module, "Clip", lambda *_: pytest.fail("orientation rescan reopened RAW"))
    for turn in (90, 180, 270, 0):
        source.with_suffix(".mcrawsome.toml").write_text(f"[develop]\nrotation={turn}\n")
        [listed] = library.scan([folder]).clips
        assert listed.display_rotation == turn
        assert (listed.width, listed.height) == (original.width, original.height)
        assert listed.id == original.id


def test_a_file_that_changes_while_it_is_hashed_is_copying(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = clip(folder / "a.mcraw")

    def growing(hashed: Path) -> str:
        digest = quick_hash(hashed)
        with hashed.open("ab") as file:  # the copy goes on while the start is read
            file.write(b"more")
        _age(hashed)
        return digest

    monkeypatch.setattr(scan_module, "quick_hash", growing)
    clock = Clock()
    library = Library(clock=clock)
    [listed] = library.scan([folder]).clips
    assert (listed.status, listed.id) == ("copying", None)
    monkeypatch.setattr(scan_module, "quick_hash", quick_hash)
    clip(path, frames=4)  # the copy finishes
    for _ in range(2):  # it changed since the last scan: it waits until it has settled
        clock.now += scan_module.SETTLE_S + 1
        [listed] = library.scan([folder]).clips
    assert listed.status == "ready"
    assert listed.id == clip_id(path)


def test_a_symlink_to_a_clip_outside_the_folder_is_not_listed(
    folder: Path, clip: MakeClip, tmp_path: Path
) -> None:
    outside = clip(tmp_path / "elsewhere" / "secret.mcraw")
    (folder / "link.mcraw").symlink_to(outside)
    inside = clip(folder / "a.mcraw", first=1)
    (folder / "alias.mcraw").symlink_to(inside)
    [listed] = scan([folder])
    assert listed.paths == [folder / "a.mcraw", folder / "alias.mcraw"]
    # the server's roots decide, as for any path it is given: one that holds the target lists the link
    names = {path.name for c in scan([folder], roots=[folder, outside.parent]) for path in c.paths}
    assert names == {"a.mcraw", "alias.mcraw", "link.mcraw"}


def test_an_exported_video_beside_the_clips_is_not_listed(folder: Path, synthetic_video: Path) -> None:
    output = Path(shutil.move(synthetic_video, folder / "a_gorgeous_ProRes.mov"))
    (folder / "a_gorgeous_ProRes.mov.mcrawsome.toml").write_text("[recipe]\n")
    _age(output)
    assert scan([folder]) == []


def test_refresh_reports_what_changed_since_the_last_scan(folder: Path, clip: MakeClip) -> None:
    clock = Clock()
    library = Library(clock=clock)
    a = clip(folder / "a.mcraw")
    _, change = library.refresh([folder])
    assert change is not None
    assert [c.name for c in change.clips] == ["a.mcraw"]
    old_id = change.clips[0].id
    assert library.refresh([folder])[1] is None  # nothing changed: nothing to say, however often asked
    # replaced through a temporary file, as a sync tool does
    temporary = folder / ".syncthing.a.mcraw.tmp"
    clip(temporary, first=5)
    assert library.refresh([folder])[1] is None  # the temporary file is not a clip
    temporary.replace(a)
    _, change = library.refresh([folder])
    assert change is not None
    assert change.removed == [old_id]
    assert [(c.name, c.status, c.id) for c in change.clips] == [("a.mcraw", "copying", None)]
    clock.now += scan_module.SETTLE_S + 1
    _, change = library.refresh([folder])
    assert change is not None
    [replaced] = change.clips
    assert (replaced.name, replaced.status) == ("a.mcraw", "ready")
    assert replaced.id not in (None, old_id)
    assert change.removed_paths == [a]  # the copying entry, now listed under its id
    a.rename(folder / "b.mcraw")
    _, change = library.refresh([folder])
    assert change is not None
    assert [(c.id, c.name) for c in change.clips] == [(replaced.id, "b.mcraw")]
    assert (change.removed, change.removed_paths) == ([], [])
    shutil.rmtree(folder)
    _, change = library.refresh([folder])
    assert change is not None
    assert change.removed == [replaced.id]
    assert change.missing_folders == [folder]
    assert library.view.clips == []


def test_details_give_the_clips_readiness_and_recipe(folder: Path, make_mcraw: MakeClip) -> None:
    timestamps = [0, STEP, 3 * STEP, 4 * STEP, 5 * STEP]  # one frame dropped
    path = make_mcraw(folder / "a.mcraw", timestamps)
    path.with_suffix(".gcsv").write_text("x")
    (folder / "a.mcrawsome.toml").write_text('look = "bond"\n')
    _age(path)
    library = Library()
    [listed] = library.scan([folder]).clips
    assert listed.badges == ["has-settings", "dropped-frames"]
    assert listed.gyro
    details = library.details(listed)
    assert (details.gyro, details.dropped_frames, details.filled_frames) == (True, 1, 1)
    assert details.recovered_note is None
    assert details.recipe.values == {"look": "bond"}
    assert details.id == listed.id
    (folder / "a.mcrawsome.toml").write_text("[output]\nfill_dropped = false\n")
    assert library.details(listed).filled_frames == 0
    path.unlink()
    with pytest.raises(McError) as missing:
        library.details(listed)
    assert missing.value.code == "clip-missing"


def test_gyro_availability_tracks_sidecars_without_reopening_raw(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = clip(folder / "imu.mcraw")
    library = Library()
    assert not library.scan([folder]).clips[0].gyro
    monkeypatch.setattr(scan_module, "_read_facts", lambda _: pytest.fail("cached RAW was reopened"))
    path.with_suffix(".gcsv").write_text("Gyro log")
    view, change = library.refresh([folder])
    assert view.clips[0].gyro
    assert change is not None
    assert change.clips[0].gyro
    path.with_suffix(".gcsv").unlink()
    view, change = library.refresh([folder])
    assert not view.clips[0].gyro
    assert change is not None
    assert not change.clips[0].gyro


def test_a_videos_capture_time_is_its_creation_time(folder: Path, synthetic_video: Path) -> None:
    import subprocess

    from mcrawsome.output.ffmpeg import find_ffmpeg

    video = folder / "b_Davinci_Davinci-WG.mp4"
    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-i", str(synthetic_video), "-c", "copy",
         "-metadata", "creation_time=2026-10-11T09:50:29.000000Z",
         "-movflags", "use_metadata_tags", str(video)],
        check=True,
    )  # fmt: skip
    _age(video)
    [listed] = scan([folder])
    assert listed.captured is not None
    assert listed.captured.isoformat() == "2026-10-11T09:50:29+00:00"


@pytest.mark.parametrize("restart", [False, True])
def test_failed_facts_are_retried_each_scan(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch, restart: bool
) -> None:
    path = clip(folder / "a.mcraw")
    (folder / "alias.mcraw").symlink_to(path)
    real = scan_module._read_facts
    calls = 0

    def failing(path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("temporary probe failure")

    monkeypatch.setattr(scan_module, "_read_facts", failing)
    library = Library()
    assert library.scan([folder]).clips[0].status == "unreadable"
    assert calls == 1
    library.scan([folder])
    assert calls == 2
    monkeypatch.setattr(scan_module, "_read_facts", real)
    if restart:
        library = Library()
    assert library.scan([folder]).clips[0].status == "ready"


def test_details_mark_a_readiness_failure_unreadable(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip(folder / "a.mcraw")
    library = Library()
    [listed] = library.scan([folder]).clips

    def failing(*args: object) -> dict[str, object]:
        raise RuntimeError("cannot read readiness")

    monkeypatch.setattr(scan_module, "_readiness", failing)
    details = library.details(listed)
    assert details.status == "unreadable"
    assert details.error == "cannot read readiness"


def test_persistent_probe_failure_does_not_rewrite_stored_facts(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip(folder / "a.mcraw")
    library = Library()
    library.scan([folder])
    saves: list[None] = []
    monkeypatch.setattr(library, "_save", lambda: saves.append(None))

    def failing(path: Path) -> dict[str, object]:
        raise RuntimeError("persistent failure")

    monkeypatch.setattr(scan_module, "_read_facts", failing)
    library._facts.clear()
    library.scan([folder])
    library.scan([folder])
    assert saves == []


def test_details_include_original_metadata_without_decoding_pixels(
    folder: Path, make_mcraw: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io.clip import Clip

    path = make_mcraw(folder / "metadata.mcraw", [i * STEP for i in range(3)])
    _age(path)
    library = Library()
    [listed] = library.scan([folder]).clips
    with Clip(path) as opened:
        expected = {
            "Container": opened.raw_metadata,
            "First frame": opened.raw_frame_metadata(0),
            "White balance": {"temperature": listed.as_shot_temperature, "tint": listed.as_shot_tint},
        }

    def no_pixels(*args: object) -> None:
        pytest.fail("reading Library metadata decoded a RAW frame")

    monkeypatch.setattr(Clip, "raw_frame", no_pixels)
    assert library.details(listed).model_dump()["metadata"] == expected


def test_video_details_include_codec_audio_and_container_metadata(
    folder: Path, synthetic_video: Path
) -> None:
    path = Path(shutil.move(synthetic_video, folder / "clip.mp4"))
    _age(path)
    library = Library()
    [listed] = library.scan([folder]).clips
    metadata = library.details(listed).metadata
    assert metadata["Video"]["width"] == 320
    assert metadata["Video"]["height"] == 180
    assert metadata["Video"]["fps"] == "24"
    assert metadata["Video"]["codec"]
    assert metadata["Video"]["path"] == str(path)
    assert isinstance(metadata["Device"], dict)


@pytest.mark.parametrize("name", ["clip.mp4", "clip_Sony S-Log3_Rec2020.mp4", "clip_BT.709_BT709.mp4"])
def test_recorded_profile_metadata_wins_over_names_and_develop_overrides(
    folder: Path, synthetic_video: Path, name: str
) -> None:
    path = Path(shutil.move(synthetic_video, folder / name))
    path.with_suffix(".mcrawsome.toml").write_text('[develop]\ninput_encoding = "acescct"\n')
    _age(path)
    library = Library()
    [listed] = library.scan([folder]).clips
    video = library.details(listed).metadata["Video"]
    assert video["colour_space"] == "DaVinci Wide Gamut"
    assert video["transfer_function"] == "DaVinci Intermediate"
    assert video["transfer_id"] == "davinci-intermediate"
    assert video["encoding_source"] == "Recording profile metadata"


def test_scan_counts_duplicate_timestamps_once_without_decoding(
    folder: Path, make_mcraw: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_mcraw(folder / "duplicates.mcraw", [0, STEP, STEP, 2 * STEP, 4 * STEP])
    _age(source)
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("scan decoded RAW"))
    [summary] = Library().scan([folder]).clips
    assert summary.duplicated_frames == 1
    assert summary.missing_frames == 1
    monkeypatch.setattr(scan_module, "Clip", lambda *_: pytest.fail("cached scan reopened RAW"))
    [cached] = Library().scan([folder]).clips
    assert cached == summary


def test_old_cached_facts_refresh_frame_counts_once(
    folder: Path, clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = clip(folder / "old.mcraw")
    Library().scan([folder])
    cache = state_dir() / "library.json"
    document = json.loads(cache.read_text())
    for facts in document["files"].values():
        facts.pop("duplicated_frames", None)
    cache.write_text(json.dumps(document))
    [summary] = Library().scan([folder]).clips
    assert summary.path == source
    assert summary.duplicated_frames == 0
    monkeypatch.setattr(scan_module, "_read_facts", lambda *_: pytest.fail("cached scan reopened media"))
    assert Library().scan([folder]).clips == [summary]


def test_log_camera_and_telemetry_facts_survive_cached_scan(
    folder: Path, synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np

    import mcrawsome.library.scan as scan_module
    from mcrawsome.io.gyro import GyroLog

    path = Path(shutil.move(synthetic_video, folder / "camera_Davinci_Davinci-WG.mp4"))
    _age(path)
    gyro = GyroLog(np.array([0.0, 1.0]), np.zeros((2, 3)), np.ones((2, 3)), None, "XYZ", clock="video")
    monkeypatch.setattr(scan_module, "read_gyro", lambda *_: (gyro, {"MINF": "Synthetic phone 25mm f/1.8"}))
    library = Library()
    first = library.scan([folder]).clips[0]
    assert first.camera == "Synthetic phone"
    assert first.focal_length_mm == 25
    assert first.aperture == 1.8
    assert first.gyro
    assert first.sensor_bits == 8
    assert first.iso is None
    assert first.exposure_time_ns is None
    detail = library.details(first)
    assert detail.metadata["Telemetry"]["gyro_samples"] == 2
    assert detail.metadata["Telemetry"]["accelerometer"] is True
    monkeypatch.setattr(scan_module, "_read_facts", lambda *_: pytest.fail("cached LOG was reopened"))
    assert library.scan([folder]).clips[0].model_dump() == first.model_dump()


def test_raw_clips_list_their_movement_from_the_gyro_log_beside_them(
    folder: Path,
    make_mcraw: Callable[..., Path],
    make_gcsv: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.analysis import motion_insights

    source = make_mcraw(folder / "walk.mcraw", [i * 41_666_667 for i in range(120)])
    _age(source)
    library = Library()
    [row] = library.scan([folder]).clips
    assert row.movement is None
    log = make_gcsv(source.with_suffix(".gcsv"), 120, shake_deg_s=12)
    _age(log)
    [row] = library.scan([folder]).clips
    assert row.movement == "handheld"
    make_gcsv(log, 120, shake_deg_s=0.2)
    _age(log, seconds=60)
    [row] = library.scan([folder]).clips
    assert row.movement == "still"

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unchanged log is not measured again")

    monkeypatch.setattr(motion_insights, "load", forbidden)
    assert Library().scan([folder]).clips[0].movement == "still"  # remembered in library.json
    log.unlink()
    assert library.scan([folder]).clips[0].movement is None


def test_a_recorded_gps_fix_lists_its_position_and_the_nearest_place(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _DATA, _payload

    from mcrawsome.io.container import ContainerWriter
    from mcrawsome.library import places

    meta = json.loads((_DATA / "container_metadata_174122.json").read_text())
    settings = meta["extraData"]["postProcessSettings"]
    settings |= {"gpsLatitude": 51.4612, "gpsLongitude": -2.5921, "gpsAltitude": 20.0}
    settings["gpsTime"] = "2026-10-25"
    frame = json.loads((_DATA / "frame_metadata_174122.json").read_text())
    frame |= {"width": 128, "height": 64, "originalWidth": 128, "originalHeight": 64, "rowStride": 256}
    source = folder / "located.mcraw"
    with ContainerWriter(source, meta) as writer:
        for index in range(3):
            stamp = index * STEP
            writer.add_frame(stamp, _payload(100), json.dumps(frame | {"timestamp": stamp}).encode())
    _age(source)
    found = places.Place(name="Bristol", country="United Kingdom", zone="Europe/London", distance_km=1.2)
    monkeypatch.setattr(places, "nearest", lambda latitude, longitude, **_: found)
    [row] = Library().scan([folder]).clips
    assert row.gps
    assert row.position == (51.4612, -2.5921)
    assert row.place == "Bristol, United Kingdom"
    assert row.place_km == 1.2


def test_movement_is_measured_a_budget_at_a_time_so_listing_stays_quick(
    folder: Path, make_mcraw: Callable[..., Path], make_gcsv: Callable[..., Path]
) -> None:
    for offset, name in enumerate(("a", "b", "c")):
        source = make_mcraw(folder / f"{name}.mcraw", [offset + i * 41_666_667 for i in range(48)])
        _age(source, make_gcsv(source.with_suffix(".gcsv"), 48, shake_deg_s=12))
    library = Library()
    library.motion_budget_s = -1.0  # spent before it starts: one clip per scan
    counts = []
    for _ in range(3):
        view = library.scan([folder])
        counts.append(sum(clip.movement is not None for clip in view.clips))
        assert len(view.clips) == 3  # every clip is listed while its movement waits
    assert counts == [1, 2, 3]
    assert not library.pending_motion


def test_inside_takes_a_folder_or_the_file_itself_as_a_root(tmp_path: Path) -> None:
    from mcrawsome.library.scan import inside

    folder = tmp_path / "library"
    nested = folder / "day" / "a.mcraw"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")
    alone = tmp_path / "alone.mcraw"
    alone.write_bytes(b"y")
    roots = [folder.resolve(), alone.resolve()]
    for given in (roots, set(roots), frozenset(roots)):
        assert inside(nested, given) == nested.resolve()
        assert inside(alone, given) == alone.resolve()
        assert inside(folder, given) == folder.resolve()
        assert inside(tmp_path / "other.mcraw", given) is None
        assert inside(tmp_path, given) is None


def test_raw_clips_with_motion_recorded_in_them_list_gyro_and_movement(
    folder: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import RECORDED_CONTAINER, RECORDED_FRAME, recorded_motion

    from mcrawsome.analysis import motion_insights

    stamps = [i * 41_666_667 for i in range(120)]
    source = make_mcraw(
        folder / "labs.mcraw",
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], 5.0, shake_deg_s=12),
    )
    _age(source)
    library = Library()
    [row] = library.scan([folder]).clips
    assert row.gyro  # no .gcsv beside it: the motion is the recording's own
    assert row.movement == "handheld"

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unchanged recording is not measured again")

    monkeypatch.setattr(motion_insights, "load", forbidden)
    [again] = Library().scan([folder]).clips  # remembered in library.json
    assert again.gyro
    assert again.movement == "handheld"
    details = library.details(row)
    assert details.gyro
    assert details.camera_profile == "Recorded in the clip"  # in place of a matching profile


def test_records_from_before_recorded_motion_read_their_raw_clips_once_more(
    folder: Path, make_mcraw: Callable[..., Path]
) -> None:
    from conftest import RECORDED_CONTAINER, RECORDED_FRAME, recorded_motion

    stamps = [i * 41_666_667 for i in range(24)]
    source = make_mcraw(
        folder / "labs.mcraw",
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], 1.0),
    )
    _age(source)
    Library().scan([folder])
    record = state_dir() / "library.json"
    saved = json.loads(record.read_text())
    for facts in saved["files"].values():  # as an older engine left it: nothing known about the motion
        facts.pop("recorded_motion", None)
        facts.pop("recorded_gravity", None)
    record.write_text(json.dumps(saved))
    [row] = Library().scan([folder]).clips
    assert row.gyro
