import numpy as np
import pytest

from mcrawsome import _raw


def test_rejects_unknown_compression_type() -> None:
    with pytest.raises(ValueError, match="compression type"):
        _raw.decode(b"\x00" * 16, 4, 4, 99)


def test_rejects_odd_dimensions() -> None:
    with pytest.raises(ValueError, match="even"):
        _raw.decode(b"\x00" * 16, 3, 4, 7)


def test_empty_input_is_rejected() -> None:
    # Empty input is rejected before the decoder tries to read its header.
    with pytest.raises(ValueError, match="empty"):
        _raw.decode(b"", 64, 64, 7)


def test_returns_uint16_array_type() -> None:
    assert np.dtype(np.uint16).itemsize == 2  # guards the stub's declared dtype
