import tracemalloc
from pathlib import Path
from typing import Any

import numpy as np
import OpenEXR
import pytest

from mcrawsome.colour.spaces import ACES_WHITE, AP1, D65, GAMUTS, REC709
from mcrawsome.output.exr import EXR_SPACES, write_exr


def _frame(height: int = 6, width: int = 8) -> np.ndarray:
    rng = np.random.default_rng(7)
    frame = rng.uniform(0.0, 4.0, (height, width, 3)).astype(np.float32)
    frame[0, 0] = (0.18, 0.18, 0.18)
    frame[0, 1] = (-0.01, 0.0, 100.0)
    return frame


def _read(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with OpenEXR.File(str(path), separate_channels=True) as f:
        return dict(f.header()), {name: ch.pixels.copy() for name, ch in f.channels().items()}


@pytest.mark.parametrize(
    ("space", "primaries", "white"), [("acescg", AP1, ACES_WHITE), ("linear-rec709", REC709, D65)]
)
@pytest.mark.parametrize("compression", ["zip", "piz"])
def test_write_then_read_back(tmp_path: Path, space: str, primaries, white, compression: str) -> None:  # type: ignore[no-untyped-def]
    frame = _frame()
    path = tmp_path / "frame.000001.exr"
    write_exr(path, frame, space=space, compression=compression)
    header, channels = _read(path)
    assert sorted(channels) == ["B", "G", "R"]
    for i, name in enumerate("RGB"):
        assert channels[name].dtype == np.float16
        np.testing.assert_array_equal(channels[name], frame[..., i].astype(np.float16))
    expected = (*primaries[0], *primaries[1], *primaries[2], *white)
    np.testing.assert_allclose(header["chromaticities"], expected, atol=1e-7)
    assert str(header["compression"]).endswith(f"{compression.upper()}_COMPRESSION")
    assert "MCRAWsome" in str(header["comment"])
    assert EXR_SPACES[space].name in str(header["comment"])


def test_values_beyond_half_range_are_clipped_not_infinite(tmp_path: Path) -> None:
    frame = np.full((2, 2, 3), 1e6, dtype=np.float32)
    frame[0, 0] = -1e6
    write_exr(tmp_path / "f.exr", frame)
    _, channels = _read(tmp_path / "f.exr")
    assert all(np.isfinite(c).all() for c in channels.values())
    assert float(channels["R"][1, 1]) == 65504.0
    assert float(channels["R"][0, 0]) == -65504.0


def test_rejects_a_frame_without_three_channels(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="height x width x 3"):
        write_exr(tmp_path / "f.exr", np.zeros((4, 4), dtype=np.float32))


def test_rejects_an_unknown_space_or_compression(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="rec2020"):
        write_exr(tmp_path / "f.exr", _frame(), space="rec2020")
    with pytest.raises(ValueError, match="dwaa"):
        write_exr(tmp_path / "f.exr", _frame(), compression="dwaa")


def _peak_while_writing(tmp_path: Path, frame: np.ndarray, frames: int) -> int:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        for i in range(frames):
            write_exr(tmp_path / f"f.{i:06d}.exr", frame)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


# tracemalloc sees the Python-side buffers (NumPy arrays and Python objects) only, not what the
# OpenEXR C++ library allocates while it compresses and writes.


def test_holds_one_frame_at_a_time(tmp_path: Path) -> None:
    frame = _frame(256, 384)
    # Four frames written in turn never hold more than about one frame's worth of buffers.
    assert _peak_while_writing(tmp_path, frame, 4) < 2 * frame.nbytes


def test_holds_only_the_half_float_copy(tmp_path: Path) -> None:
    frame = _frame(256, 384).astype(np.float64)
    # The only Python-side copy is the half-float one, a quarter of a float64 frame.
    assert _peak_while_writing(tmp_path, frame, 2) < 0.4 * frame.nbytes


def test_output_spaces_are_the_registered_gamuts() -> None:
    assert EXR_SPACES["acescg"].gamut is GAMUTS["ap1"]
    assert EXR_SPACES["linear-rec709"].gamut is GAMUTS["rec709"]
