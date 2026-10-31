from pathlib import Path

import numpy as np
import pytest

from mcrawsome.motion.optical import FeatureTracks
from mcrawsome.motion.optical_cache import measured


def test_tracks_reuse_independent_of_plan_but_invalidate_for_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.motion import optical_cache

    monkeypatch.setattr(optical_cache, "cache_dir", lambda: tmp_path / "cache")
    source = tmp_path / "clip.mcraw"
    source.write_bytes(b"original recording")
    calls = []

    def compute() -> FeatureTracks:
        calls.append(True)
        return FeatureTracks(
            np.array([0, 1]),
            np.array([2, 2]),
            np.full((2, 2), 50, np.float32),
            np.full((2, 2), 51, np.float32),
            480,
            270,
        )

    first = measured(source, 3, compute, lambda: None)
    again = measured(source, 3, compute, lambda: None)
    np.testing.assert_array_equal(first.before, again.before)
    assert len(calls) == 1
    source.write_bytes(b"changed recording")
    measured(source, 3, compute, lambda: None)
    assert len(calls) == 2
    for path in (tmp_path / "cache" / "optical-tracks").glob("*.npz"):
        path.write_bytes(b"incomplete cache")
    measured(source, 3, compute, lambda: None)
    assert len(calls) == 3


def test_unwritable_cache_does_not_prevent_analysis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.motion import optical_cache

    source = tmp_path / "source"
    source.write_bytes(b"source")
    monkeypatch.setattr(optical_cache, "cache_dir", lambda: source)
    expected = FeatureTracks(
        np.array([0]), np.array([0]), np.ones((1, 2), np.float32), np.ones((1, 2), np.float32), 480, 270
    )
    assert measured(source, 2, lambda: expected, lambda: None) is expected
