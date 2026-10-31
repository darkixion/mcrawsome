import base64
import threading
from concurrent.futures import CancelledError
from pathlib import Path

import cv2
import numpy as np
import pytest

from mcrawsome.analysis.clipping import clipping_cells, measure_clipping
from mcrawsome.io.clip import Clip, RawFrame
from mcrawsome.recipe.load import build_recipe


@pytest.mark.parametrize("pattern", ["rggb", "bggr", "grbg", "gbrg"])
@pytest.mark.parametrize("quad", [False, True])
def test_sensor_threshold_and_any_versus_all_rgb_channels(pattern: str, quad: bool) -> None:
    block = 4 if quad else 2
    data = np.full((block * 2, block * 2), 999, np.uint16)
    assert not clipping_cells(data, pattern, 1000, "any", quad=quad).any()
    for colour in "rgb":
        site = pattern.index(colour)
        y, x = divmod(site, 2)
        data[y * (2 if quad else 1), x * (2 if quad else 1)] = 1000
        any_mask = clipping_cells(data, pattern, 1000, "any", quad=quad)
        assert any_mask[:block, :block].all()
        assert not any_mask[block:, :].any()
        assert not any_mask[:, block:].any()
        all_mask = clipping_cells(data, pattern, 1000, "all", quad=quad)
        assert bool(all_mask[:block, :block].all()) == (colour == "b")


def test_clipping_geometry_uses_sensor_crop_and_saved_rotation(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Clip.raw_frame

    def frame(clip: Clip, index: int) -> RawFrame:
        raw = original(clip, index)
        data = np.zeros_like(raw.data)
        data[:2, :2] = raw.metadata.white_level
        return RawFrame(index, raw.timestamp_ns, data, raw.metadata)

    monkeypatch.setattr(Clip, "raw_frame", frame)
    recipe = build_recipe([{"develop": {"crop": "64x32", "rotation": 90}}])
    result = measure_clipping(tiny_raw, recipe, 0, "all", 1280, threading.Event())
    image = cv2.imdecode(
        np.frombuffer(base64.b64decode(result.url.split(",")[1]), np.uint8), cv2.IMREAD_UNCHANGED
    )
    assert image is not None
    assert image.shape == (64, 32, 4)
    assert image[:2, -2:, 3].all()
    assert np.count_nonzero(image[..., 3]) == 4
    assert result.source_index == result.frame_index == 0


def test_clipping_threshold_is_recorded_white_independent_of_saved_levels(tiny_raw: Path) -> None:
    plain = measure_clipping(tiny_raw, build_recipe([]), 0, "any", 16, threading.Event())
    changed = measure_clipping(
        tiny_raw,
        build_recipe([{"develop": {"black_level": 1, "white_level": 200}}]),
        0,
        "any",
        16,
        threading.Event(),
    )
    assert changed.url == plain.url
    assert changed.white_level == plain.white_level
    assert changed.width == 16


def test_clipping_honours_cancellation_before_reading(tiny_raw: Path) -> None:
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        measure_clipping(tiny_raw, build_recipe([]), 0, "any", 1280, stop)
