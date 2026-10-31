from pathlib import Path

import numpy as np
import pytest

from mcrawsome.motion import flow
from mcrawsome.paths import cache_dir


def test_reuses_exact_motion_measurements_and_invalidates_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mcraw"
    source.write_bytes(b"synthetic")
    calls: list[list[int]] = []
    enabled = True
    monkeypatch.setattr("mcrawsome.io.clip.remosaic_enabled", lambda: enabled)

    def measure(path: Path, indices: list[int], workers: int, strips: object) -> list[flow.MotionTrack]:
        calls.append(list(indices))
        return [flow.MotionTrack(np.array([indices[0]], np.int64), *[np.array([0.125])] * 3)]

    monkeypatch.setattr(flow, "measure_strips_raw", measure)
    first = flow.measure_motion_raw(source, [0, 1], 2)
    reused = flow.measure_motion_raw(source, [0, 1], 2)
    assert len(calls) == 1
    np.testing.assert_array_equal(first.dx, reused.dx)
    reused.dx[0] = 99
    assert flow.measure_motion_raw(source, [0, 1], 2).dx[0] == 0.125
    flow.measure_motion_raw(source, [2, 3], 2)
    assert len(calls) == 2
    source.write_bytes(b"changed source")
    flow.measure_motion_raw(source, [0, 1], 2)
    assert len(calls) == 3
    enabled = False
    flow.measure_motion_raw(source, [0, 1], 2)
    assert len(calls) == 4
    for path in (cache_dir() / "motion-tracks").glob("*.json"):
        path.write_text("invalid cache")
    flow.measure_motion_raw(source, [0, 1], 2)
    assert len(calls) == 5


def test_measurement_still_works_when_cache_folder_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mcraw"
    source.write_bytes(b"synthetic")
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"file")
    monkeypatch.setenv("MCRAWSOME_CACHE_DIR", str(blocked))
    expected = flow.MotionTrack(np.array([0], np.int64), *[np.array([0.25])] * 3)
    monkeypatch.setattr(flow, "measure_strips_raw", lambda *args: [expected])
    assert flow.measure_motion_raw(source, [0, 1], 2) is expected


def test_track_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.motion import flow_cache

    monkeypatch.setattr(flow_cache, "LIMIT", 2)
    source = tmp_path / "source.mcraw"
    source.write_bytes(b"synthetic")

    def measure(path: Path, indices: list[int], workers: int, strips: object) -> list[flow.MotionTrack]:
        return [flow.MotionTrack(np.array([indices[0]], np.int64), *[np.array([0.125])] * 3)]

    monkeypatch.setattr(flow, "measure_strips_raw", measure)
    for start in range(3):
        flow.measure_motion_raw(source, [start, start + 1], 2)
    assert len(list((cache_dir() / "motion-tracks").glob("*.json"))) == 2
