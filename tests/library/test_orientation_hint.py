import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from mcrawsome.analysis.orientation import _prefix
from mcrawsome.camera.profile import find_profile
from mcrawsome.io.clip import Clip
from mcrawsome.library import scan


def log(path: Path) -> None:
    path.write_text(
        "GYROFLOW IMU LOG\ntscale,0.01\ngscale,1\nascale,0.00000001\n"
        "orientation,XYZ\nt,gx,gy,gz,ax,ay,az\n" + "".join(f"{i},0,0,0,0,980665000,0\n" for i in range(600))
    )


def test_a_listing_after_a_restart_reuses_remembered_hints_until_their_inputs_change(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_mcraw(tmp_path / "sideways.mcraw", [0, 41_666_667, 83_333_334])
    os.utime(source, (time.time() - 60,) * 2)
    log(source.with_suffix(".gcsv"))
    profile = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 24)
    assert profile is not None
    monkeypatch.setattr(scan, "find_profile", lambda *_: profile)
    _prefix.cache_clear()
    [first] = scan.Library().scan([tmp_path]).clips
    assert first.orientation_hint is not None
    from mcrawsome.analysis.orientation import check

    read: list[Path] = []

    def counted(path: Path, *args: object) -> object:
        read.append(path)
        return check(path, *args)  # type: ignore[arg-type]

    monkeypatch.setattr("mcrawsome.library.scan.check_orientation", counted)
    [again] = scan.Library().scan([tmp_path]).clips  # a new engine: only library.json remembers
    assert again.orientation_hint == first.orientation_hint
    assert read == [], "a remembered hint read the gyro log again"
    gcsv = source.with_suffix(".gcsv")
    gcsv.write_text(gcsv.read_text() + "600,0,0,0,0,980665000,0\n")  # the log changed
    assert scan.Library().scan([tmp_path]).clips[0].orientation_hint is not None
    assert read == [gcsv]
    axes = profile.gcsv_gravity_axes
    assert axes is not None
    flipped = profile.model_copy(update={"gcsv_gravity_axes": axes.model_copy(update={"signs": (-1, -1, 1)})})
    monkeypatch.setattr(scan, "find_profile", lambda *_: flipped)  # a saved camera profile changed
    scan.Library().scan([tmp_path])
    assert read == [gcsv, gcsv]


def test_library_marks_sideways_clip_and_clears_when_the_saved_turn_matches(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_mcraw(tmp_path / "sideways.mcraw", [0, 41_666_667, 83_333_334])
    os.utime(source, (time.time() - 60,) * 2)
    log(source.with_suffix(".gcsv"))
    # Synthetic tiny images use the same known coordinate mapping without claiming
    # the production 4K profile matches their resolution.
    profile = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 24)
    assert profile is not None
    monkeypatch.setattr(scan, "find_profile", lambda *_: profile)
    _prefix.cache_clear()
    library = scan.Library()
    [row] = library.scan([tmp_path]).clips
    assert row.orientation_hint is not None
    assert row.orientation_hint.appears_wrong
    assert row.orientation_hint.suggested_rotation == 90
    monkeypatch.setattr(
        scan, "find_profile", lambda *_: profile.model_copy(update={"gcsv_gravity_camera_ids": ("other",)})
    )
    assert library.scan([tmp_path]).clips[0].orientation_hint is None
    monkeypatch.setattr(scan, "find_profile", lambda *_: profile)

    def forbidden(*args: object) -> None:
        raise AssertionError("orientation hints must reuse cached clip facts without decoding")

    monkeypatch.setattr(Clip, "raw_frame", forbidden)
    monkeypatch.setattr(scan, "_read_facts", forbidden)
    source.with_suffix(".mcrawsome.toml").write_text("[develop]\nrotation = 90\n")
    [corrected] = library.scan([tmp_path]).clips
    assert corrected.orientation_hint is not None
    assert not corrected.orientation_hint.appears_wrong
    assert row.id == corrected.id
    source.with_suffix(".gcsv").unlink()
    assert library.scan([tmp_path]).clips[0].orientation_hint is None
