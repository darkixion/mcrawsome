import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from mcrawsome.io.clip import Clip
from mcrawsome.library.scan import Library


def test_capture_details_are_available_in_rows_without_decoding(
    tmp_path: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = make_mcraw(tmp_path / "details.mcraw", [0, 41_666_667, 83_333_334])
    os.utime(path, (time.time() - 60,) * 2)
    with Clip(path) as clip:
        first = clip.frame_metadata(0)
        focal = clip.metadata.focal_lengths[0]
        aperture = clip.metadata.apertures[0]

    def forbidden(*args: object) -> None:
        raise AssertionError("decoded pixels for a row")

    monkeypatch.setattr(Clip, "raw_frame", forbidden)
    [row] = Library().scan([tmp_path]).clips
    assert row.frame_count == 3
    assert row.measured_fps == pytest.approx(24)
    assert row.iso == first.iso
    assert row.focal_length_mm == focal
    assert row.aperture == aperture
    assert row.exposure_time_ns == first.exposure_time_ns
    assert row.white_level == first.white_level
    assert row.black_levels == first.black_level


def test_rows_include_resolved_sources_for_mount_status(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    source = make_mcraw(tmp_path / "original.mcraw", [0, 41_666_667, 83_333_334])
    os.utime(source, (time.time() - 60,) * 2)
    (tmp_path / "alias.mcraw").symlink_to(source)
    [row] = Library().scan([tmp_path]).clips
    assert row.canonical_paths == [source]
    assert len(row.paths) == 2
