"""Full-quality device development must keep the CPU export's colour contract."""

import os
from collections.abc import Iterator

import numpy as np
import pytest
from test_raw import CONTAINER, NEUTRAL, _frame

from mcrawsome.develop.raw import Developer
from mcrawsome.preview.opencl import OpenCL


@pytest.fixture
def device() -> Iterator[OpenCL]:
    if os.environ.get("MCRAWSOME_OPENCL_TESTS") != "1":
        pytest.skip("needs an OpenCL GPU (set MCRAWSOME_OPENCL_TESTS=1)")
    selected = OpenCL()
    try:
        yield selected
    finally:
        selected.close()


@pytest.mark.parametrize("cfa", ["rggb", "bggr", "grbg", "gbrg"])
@pytest.mark.parametrize("orientation", [0, 3])
@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0])
def test_full_quality_matches_the_export_developer(
    device: OpenCL, cfa: str, orientation: int, strength: float
) -> None:
    container = CONTAINER.model_copy(update={"sensor_arrangement": cfa})
    developer = Developer(container, _frame(orientation), NEUTRAL, strength, black_level=(200, 230, 250, 270))
    yy, xx = np.mgrid[:16, :32].astype(np.float32)
    developer.gain *= 0.8 + 0.4 * xx / 32 + 0.1 * yy / 16
    raw = np.random.default_rng(42).integers(300, 3900, (16, 32), dtype=np.uint16)
    raw[2:6, 2:8] = 4095
    raw[8:14:2, 16:28:2] = 4095
    expected = developer.develop(raw)
    actual = device.develop_full(developer, raw)
    np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=3e-6)
    allocations = sum(size for size, _ in device.buffers.values())
    for _ in range(3):
        device.develop_full(developer, raw)
    assert sum(size for size, _ in device.buffers.values()) == allocations


@pytest.mark.parametrize("stops", [-2.0, 0.8])
def test_full_development_applies_exposure_on_the_device(device: OpenCL, stops: float) -> None:
    developer = Developer(CONTAINER, _frame(), NEUTRAL)
    raw = np.random.default_rng(2).integers(300, 4095, (16, 32), dtype=np.uint16)
    expected = developer.develop(raw) * np.float32(2**stops)
    np.testing.assert_allclose(
        device.develop_full(developer, raw, stops=stops), expected, atol=3e-6, rtol=3e-6
    )


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("pattern", ["edges", "random"])
def test_packed_highlight_neighbourhoods_keep_borders_and_channel_masks(
    device: OpenCL, full: bool, pattern: str
) -> None:
    import cv2

    height, width = 37, 81
    rng = np.random.default_rng(38)
    masks = np.zeros((height, width, 3), np.uint8)
    sites = np.zeros((height, width), np.uint8)
    if pattern == "edges":
        masks[0, 0] = (1, 0, 0)
        masks[-1, -1] = 1
        masks[0, -1] = (0, 1, 1)
        sites[-1, 0] = 1
    else:
        masks[:] = rng.random(masks.shape) > 0.96
        masks[12:15, 40:43] = 1
        sites[:] = rng.random(sites.shape) > 0.97
    flags = device.buffer("test_flags", width * height)
    ring = device.buffer("test_ring", width * height)
    white = device.buffer("test_white", width * height)
    device.launch(
        "highlight_flags",
        [device.upload("test_masks", masks), device.upload("test_sites", sites), flags],
        [int(full)],
        width,
        height,
    )
    device.launch("highlight_horizontal", [flags, ring, white, flags], [int(full), 0], width, height)
    actual_ring, actual_white = np.empty_like(sites), np.empty_like(sites)
    device.read(ring, actual_ring)
    device.read(white, actual_white)
    any_clip = masks.any(axis=2).astype(np.uint8)
    all_clip = masks.all(axis=2).astype(np.uint8)
    kernel = np.ones((1, 5 if full else 3), np.uint8)
    expected_ring = cv2.dilate(any_clip, np.ones((1, 17), np.uint8))
    expected_white = np.asarray(cv2.dilate(all_clip, kernel), np.uint8) | (
        np.asarray(cv2.dilate(sites if full else any_clip, kernel), np.uint8) << 1
    )
    np.testing.assert_array_equal(actual_ring, expected_ring)
    np.testing.assert_array_equal(actual_white, expected_white)


@pytest.mark.parametrize("cfa", ["rggb", "bggr", "grbg", "gbrg"])
@pytest.mark.parametrize("shape", [(2, 2), (6, 4), (18, 34)])
def test_specialised_malvar_keeps_small_frame_and_shifted_borders(
    device: OpenCL, cfa: str, shape: tuple[int, int]
) -> None:
    container = CONTAINER.model_copy(update={"sensor_arrangement": cfa})
    height, width = shape
    frame = _frame().model_copy(update={"width": width, "height": height})
    developer = Developer(container, frame, NEUTRAL)
    raw = np.random.default_rng(19).integers(0, 4000, shape, dtype=np.uint16)
    np.testing.assert_allclose(
        device.develop_full(developer, raw), developer.develop(raw), atol=3e-6, rtol=3e-6
    )
    assert "malvar_kernels" not in device.buffers, "fixed filters should compile into the kernel"


@pytest.mark.parametrize("cfa", ["rggb", "bggr", "grbg", "gbrg"])
@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0])
def test_sparse_highlight_tiles_keep_support_and_clear_between_frames(
    device: OpenCL, cfa: str, strength: float
) -> None:
    import cv2

    height, width = 130, 162
    container = CONTAINER.model_copy(update={"sensor_arrangement": cfa})
    frame = _frame(3).model_copy(update={"width": width, "height": height})
    developer = Developer(container, frame, NEUTRAL, strength)
    rng = np.random.default_rng(73)
    for pattern in ("none", "tiles", "edges", "dense", "none"):
        raw = rng.integers(300, 3900, (height, width), dtype=np.uint16)
        if pattern == "tiles":
            # Cross both 16-pixel tile boundaries and the 15-pixel recovery support.
            raw[31:34, 47:50] = 4095
            raw[95, 111] = 4095
        elif pattern == "edges":
            raw[0, 0] = raw[-1, -1] = raw[0, -1] = raw[-1, 0] = 4095
        elif pattern == "dense":
            raw[rng.random(raw.shape) > 0.1] = 4095
        np.testing.assert_allclose(
            device.develop_full(developer, raw), developer.develop(raw), atol=3e-6, rtol=3e-6
        )
        yy, xx = np.nonzero(raw >= 4093)
        if not len(yy):
            continue  # nothing clipped: no recovery, so no tiles to scan
        expected = np.zeros(((height + 15) // 16, (width + 15) // 16), np.uint8)
        expected[yy // 16, xx // 16] = 1
        expected = np.asarray(cv2.dilate(expected, np.ones((3, 3), np.uint8)), np.uint8)
        assert "highlight_tiles" in device.buffers, "recovery still scans unclipped tiles"
        assert device.buffers["highlight_tiles"][0] == expected.nbytes
        actual = np.empty_like(expected)
        device.read(device.buffers["highlight_tiles"][1], actual)
        np.testing.assert_array_equal(actual, expected)


CASES = ["odd", "even", "ties", "split", "invalid", "zeros", "inf", "one", "two", "none", "spread", "large"]


def _reference_ratios(case: str) -> np.ndarray:
    """Reference ratios as highlight_references writes them: -1 where the opposed channels are dark."""
    rng = np.random.default_rng(CASES.index(case))
    values: np.ndarray
    if case in ("odd", "even", "large"):
        values = rng.gamma(2.0, 0.4, ({"odd": 1001, "even": 1000, "large": 300_000}[case], 3))
    elif case == "ties":
        values = rng.integers(0, 5, (1000, 3)) * 0.25  # the two middle values are often equal
    elif case == "split":
        values = np.repeat([[0.5, 0.75, 3.0], [2.0, 0.875, 3.5]], 500, axis=0)  # middle values far apart
    elif case == "invalid":
        values = rng.gamma(2.0, 0.4, (999, 3))
        values[rng.random(values.shape) < 0.4] = -1.0
        values[:, 1] = -1.0  # a channel with no valid ratio keeps 1
    elif case == "zeros":
        values = np.concatenate([np.full((300, 3), -0.0), np.zeros((301, 3)), rng.random((500, 3))])
    elif case == "inf":
        values = np.concatenate([np.full((600, 3), np.inf), rng.random((600, 3))])
    elif case == "spread":
        values = 10.0 ** rng.uniform(-30, 30, (2000, 3))
    else:
        values = rng.random(({"one": 1, "two": 2, "none": 0}[case], 3))
    return np.asarray(values, np.float32)


@pytest.mark.parametrize("case", CASES)
def test_reference_ratio_medians_on_the_device_match_numpy(device: OpenCL, case: str) -> None:
    from mcrawsome.preview.opencl import MEDIAN_BINS

    reference = _reference_ratios(case)
    expected = np.ones(3, np.float32)
    for c in range(3):
        valid = reference[:, c][reference[:, c] >= 0]
        if len(valid):
            expected[c] = np.median(valid)
    values = device.upload("highlight_values", np.vstack([reference, np.zeros((1, 3), np.float32)]))
    counter = device.buffer("highlight_count", 4)
    bins, state = device.buffer("median_bins", 3 * MEDIAN_BINS * 4), device.buffer("median_state", 60)
    ratios = device.buffer("highlight_ratios", 12)
    for _ in range(2):  # a second frame starts from cleared bins
        device.launch_groups("median_clear", [bins, state, counter], [], 3 * MEDIAN_BINS)
        device.upload("highlight_count", np.array([len(reference)], np.uint32))
        device._medians(values, counter, bins, state, ratios)
        actual = np.empty(3, np.float32)
        device.read(ratios, actual)
        np.testing.assert_array_equal(actual, expected)


def test_a_clipped_frame_waits_for_the_device_only_at_its_end(
    device: OpenCL, monkeypatch: pytest.MonkeyPatch
) -> None:
    developer = Developer(CONTAINER, _frame(), NEUTRAL)
    raw = np.random.default_rng(9).integers(300, 3900, (16, 32), dtype=np.uint16)
    raw[4:12, 8:20] = 4095
    expected = developer.develop(raw)
    reads, read = [], device.read

    def counted(handle: object, data: np.ndarray) -> None:
        reads.append(data.nbytes)
        read(handle, data)

    monkeypatch.setattr(device, "read", counted)
    np.testing.assert_allclose(device.develop_full(developer, raw), expected, atol=3e-6, rtol=3e-6)
    assert reads == [expected.nbytes]
    assert device.staged == []  # the frame's queued writes are done and released


def test_full_development_packs_masks_in_the_demosaic(
    device: OpenCL, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    developer = Developer(CONTAINER, _frame(), NEUTRAL)
    raw = np.random.default_rng(7).integers(300, 4096, (16, 32), dtype=np.uint16)
    launch = device.launch

    def fused(name: str, buffers: list[Any], scalars: list[int | float], width: int, height: int) -> None:
        assert name != "highlight_flags", "full development should pack masks while demosaicing"
        launch(name, buffers, scalars, width, height)

    monkeypatch.setattr(device, "launch", fused)
    np.testing.assert_allclose(
        device.develop_full(developer, raw), developer.develop(raw), atol=3e-6, rtol=3e-6
    )
