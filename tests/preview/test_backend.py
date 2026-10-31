"""Backend selection stays optional and failures latch to CPU for the session."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mcrawsome import _raw
from mcrawsome.develop.raw import Developer
from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata
from mcrawsome.preview import backend
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import build_recipe

_DATA = Path(__file__).parents[1] / "data"
CONTAINER = parse_container_metadata(json.loads((_DATA / "container_metadata_174122.json").read_text()))
NEUTRAL = (0.5, 1.0, 0.6)


def _frame() -> FrameMetadata:
    data = json.loads((_DATA / "frame_metadata_174122.json").read_text())
    return FrameMetadata.model_validate(data | {"width": 32, "height": 16})


def _grey_mosaic(level: float) -> np.ndarray:
    return np.full((16, 32), round(level * 1000), np.uint16)


@pytest.mark.parametrize(
    "failure", [OSError("no loader"), RuntimeError("no device"), RuntimeError("build error")]
)
def test_initialisation_failure_latches_to_cpu(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    calls = 0

    def unavailable() -> Any:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(backend, "OpenCL", unavailable)
    selected = backend.PreviewBackend()
    developer, raw = Developer(CONTAINER, _frame(), NEUTRAL), _grey_mosaic(0.2)
    for _ in range(2):
        np.testing.assert_array_equal(selected.develop(developer, raw, 2, 16), developer.develop_fast(raw))
    assert selected.name == "CPU"
    assert calls == 1
    assert str(failure) in selected.reason
    assert "CPU" in caplog.text
    selected.close()


@pytest.mark.parametrize("stage", ["develop", "finish"])
def test_device_loss_retries_the_same_frame_and_never_reopens(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    closed: list[bool] = []

    class Lost:
        device_name = "test GPU"

        def develop(self, dev: Developer, raw: np.ndarray, step: int, width: int) -> np.ndarray:
            if stage == "develop":
                raise RuntimeError("device lost during readback")
            return dev.develop_fast(raw, step, width)

        def finish(self, linear: np.ndarray, table: np.ndarray, stops: float) -> np.ndarray:
            raise RuntimeError("device lost during look")

        def close(self) -> None:
            closed.append(True)
            raise RuntimeError("release after device loss")

    monkeypatch.setattr(backend, "OpenCL", Lost)
    selected = backend.PreviewBackend()
    dev, raw = Developer(CONTAINER, _frame(), NEUTRAL), _grey_mosaic(0.2)
    linear = selected.develop(dev, raw, 2, 16)
    table = np.zeros((8, 3), np.float32)
    np.testing.assert_array_equal(selected.finish(linear, table, 0), _raw.apply_preview(linear, table, 0))
    assert selected.name == "CPU"
    selected.develop(dev, raw, 2, 16)
    selected.close()
    assert closed == [True]


def test_setting_disables_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected() -> Any:
        pytest.fail("disabled backend loaded OpenCL")

    monkeypatch.setattr(backend, "OpenCL", unexpected)
    selected = backend.PreviewBackend(enabled=False)
    assert selected.name == "CPU"
    assert "disabled" in selected.reason
    dev = Developer(CONTAINER, _frame(), NEUTRAL)
    selected.develop(dev, _grey_mosaic(0.2), 2, 16)
    selected.close()


def test_session_uses_gpu_for_draft_and_slider_frames(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Device:
        device_name = "test GPU"

        def develop(self, dev: Developer, raw: np.ndarray, step: int, width: int) -> np.ndarray:
            calls.append("develop")
            return dev.develop_fast(raw, step, width)

        def finish(self, linear: np.ndarray, table: np.ndarray, stops: float) -> np.ndarray:
            calls.append("finish")
            return _raw.apply_preview(linear, table, stops)

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setenv("MCRAWSOME_PREVIEW_GPU", "1")
    monkeypatch.setattr(backend, "OpenCL", Device)
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    with PreviewSession(clip, build_recipe([]), PreviewSettings(gpu=True)) as session:
        session.fast(0)
        session.set_recipe(build_recipe([{"develop": {"exposure": 1}}]), fast=True)
        session.fast(0)
        assert session.backend.name == "OpenCL: test GPU"
    assert calls == ["develop", "finish", "finish", "close"]


def test_requested_fast_width_reaches_the_worker(tiny_raw: Path) -> None:
    from multiprocessing.connection import Connection
    from typing import cast

    from mcrawsome.preview.worker import _Loop

    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(fast_width=1920, gpu=False)) as session:
        loop = _Loop(cast(Connection, object()), session)
        try:
            assert loop.playback_width == 1920
        finally:
            loop.close()


@pytest.mark.parametrize("strength", [0.0, 0.2, 0.5, 0.9, 1.0])
def test_real_device_recovers_highlights_without_host_image_processing(
    monkeypatch: pytest.MonkeyPatch,
    strength: float,
) -> None:
    import os

    from mcrawsome.preview.opencl import OpenCL

    if os.environ.get("MCRAWSOME_OPENCL_TESTS") != "1":
        pytest.skip("needs an OpenCL GPU (set MCRAWSOME_OPENCL_TESTS=1)")
    dev = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=strength)
    raw = np.random.default_rng(3).integers(0, 1024, (16, 32), dtype=np.uint16)
    raw[4:12, 8:24] = 1023
    expected = dev.develop_fast(raw, 2, 16)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("the GPU recovered a full RGB image on the host")

    monkeypatch.setattr(Developer, "recover_preview", unexpected)
    device = OpenCL()
    try:
        np.testing.assert_allclose(device.develop(dev, raw, 2, 16), expected, atol=3e-6, rtol=3e-6)
    finally:
        device.close()


@pytest.mark.parametrize("operation", ["upload", "launch", "read"])
def test_real_device_transfer_and_dispatch_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import os

    from mcrawsome.preview.opencl import OpenCL

    if os.environ.get("MCRAWSOME_OPENCL_TESTS") != "1":
        pytest.skip("needs an OpenCL GPU (set MCRAWSOME_OPENCL_TESTS=1)")
    selected = backend.PreviewBackend()
    assert selected._device() is not None, selected.reason

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"injected {operation} failure")

    monkeypatch.setattr(OpenCL, operation, fail)
    dev, raw = Developer(CONTAINER, _frame(), NEUTRAL), _grey_mosaic(0.2)
    np.testing.assert_array_equal(selected.develop(dev, raw, 2, 16), dev.develop_fast(raw, 2, 16))
    assert selected.name == "CPU"
    assert selected.reason == f"injected {operation} failure"
    selected.close()


def test_device_warmup_does_not_block_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()
    closed: list[bool] = []

    class Device:
        device_name = "warming GPU"

        def __init__(self) -> None:
            entered.set()
            assert release.wait(5)

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(backend, "OpenCL", Device)
    selected = backend.PreviewBackend()
    try:
        selected.prepare()
        assert entered.wait(3)
        dev, raw = Developer(CONTAINER, _frame(), NEUTRAL), _grey_mosaic(0.2)
        np.testing.assert_array_equal(selected.develop(dev, raw, 2, 16), dev.develop_fast(raw, 2, 16))
        assert not release.is_set()
    finally:
        release.set()
        selected.close()
    assert closed == [True]


def test_full_resolution_cancellation_keeps_the_device(monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import CancelledError

    class Device:
        device_name = "test GPU"
        cancelled: Callable[[], bool] | None = None

        def preview_full(self, *args: Any) -> np.ndarray:
            assert self.cancelled is not None
            raise CancelledError()

        def close(self) -> None:
            pass

    monkeypatch.setattr(backend, "OpenCL", Device)
    selected = backend.PreviewBackend()
    selected.cancelled = lambda: False
    try:
        developer, raw = Developer(CONTAINER, _frame(), NEUTRAL), _grey_mosaic(0.2)
        table = np.zeros((8, 3), np.float32)
        with pytest.raises(CancelledError):
            selected.preview_full(developer, raw, table, 0.0)
        assert selected.name == "OpenCL: test GPU"
        assert selected.reason == "GPU preview active"
    finally:
        selected.close()


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("strength", [0.0, 0.4, 1.0])
@pytest.mark.parametrize("rotate", [False, True])
def test_real_device_skips_spatial_recovery_when_no_sensor_site_clips(
    monkeypatch: pytest.MonkeyPatch, full: bool, strength: float, rotate: bool
) -> None:
    import os

    from mcrawsome.preview.opencl import OpenCL

    if os.environ.get("MCRAWSOME_OPENCL_TESTS") != "1":
        pytest.skip("needs an OpenCL GPU (set MCRAWSOME_OPENCL_TESTS=1)")
    dev = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=strength)
    dev.rotate_180 = rotate
    raw = np.random.default_rng(8).integers(100, 400, (16, 32), dtype=np.uint16)
    expected = dev.develop(raw) if full else dev.develop_fast(raw, 2, 16)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unclipped playback ran spatial highlight recovery")

    monkeypatch.setattr(OpenCL, "_highlights", unexpected)
    device = OpenCL()
    try:
        actual = device.develop_full(dev, raw) if full else device.develop(dev, raw, 2, 16)
        np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=3e-6)
    finally:
        device.close()
