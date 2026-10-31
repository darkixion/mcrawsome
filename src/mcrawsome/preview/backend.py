"""One optional compute device per preview session, with a permanent CPU fallback."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor

import numpy as np
import numpy.typing as npt

from mcrawsome import _raw
from mcrawsome.develop.raw import Developer, Image
from mcrawsome.preview.opencl import OpenCL

log = logging.getLogger(__name__)


def gpu_enabled() -> bool:
    """The environment override is useful for diagnosis and applies to child workers too."""
    return os.environ.get("MCRAWSOME_PREVIEW_GPU", "1").lower() not in ("0", "false", "off")


class PreviewBackend:
    def __init__(self, enabled: bool = True) -> None:
        self.cancelled: Callable[[], bool] | None = None
        self.device: OpenCL | None = None
        self.reason = "not initialised" if enabled else "disabled by setting"
        self._attempt = enabled
        self._preparing: Future[OpenCL] | None = None
        self._loader: ThreadPoolExecutor | None = None

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise CancelledError()

    @property
    def name(self) -> str:
        return f"OpenCL: {self.device.device_name}" if self.device is not None else "CPU"

    @property
    def timings(self) -> dict[str, float]:
        return dict(getattr(self.device, "timings", {})) if self.device is not None else {}

    def begin_frame(self) -> None:
        if self.device is not None:
            getattr(self.device, "timings", {}).clear()

    def prepare(self) -> None:
        """Compile and open the device off the request thread; CPU drafts work meanwhile."""
        if self._attempt:
            self._attempt = False
            self.reason = "GPU preview warming"
            self._loader = ThreadPoolExecutor(1, thread_name_prefix="preview-device")
            self._preparing = self._loader.submit(OpenCL)

    def _device(self) -> OpenCL | None:
        if self._preparing is not None:
            if not self._preparing.done():
                return None
            task, self._preparing = self._preparing, None
            try:
                self.device = task.result()
            except Exception as exc:
                self._fallback(exc)
            else:
                self.reason = "GPU preview active"
        if self._attempt:
            self._attempt = False
            try:
                self.device = OpenCL()
                self.device.cancelled = self.cancelled
            except Exception as exc:
                self._fallback(exc)
            else:
                self.reason = "GPU preview active"
                log.info("preview backend: %s", self.name)
        return self.device

    def _fallback(self, error: Exception) -> None:
        self.reason = str(error)
        log.warning("preview backend: CPU for this session (%s)", error)
        self.close()

    def develop(self, developer: Developer, raw: npt.NDArray[np.uint16], step: int, width: int) -> Image:
        self.check_cancelled()
        device = self._device()
        if device is not None:
            device.cancelled = self.cancelled
            try:
                return device.develop(developer, raw, step, width)
            except CancelledError:
                raise
            except Exception as exc:
                self._fallback(exc)
        return developer.develop_fast(raw, step, width)

    def finish(self, linear: Image, table: Image, stops: float) -> npt.NDArray[np.uint8]:
        self.check_cancelled()
        device = self._device()
        if device is not None:
            device.cancelled = self.cancelled
            try:
                return device.finish(linear, table, stops)
            except CancelledError:
                raise
            except Exception as exc:
                self._fallback(exc)
        return _raw.apply_preview(linear, table, stops)

    def preview_full(
        self, developer: Developer, raw: npt.NDArray[np.uint16], table: Image, stops: float
    ) -> npt.NDArray[np.uint8]:
        self.check_cancelled()
        device = self._device()
        if device is not None:
            device.cancelled = self.cancelled
            try:
                return device.preview_full(developer, raw, table, stops)
            except CancelledError:
                raise
            except Exception as exc:
                self._fallback(exc)
        linear = developer.develop(raw)
        self.check_cancelled()
        return _raw.apply_preview(linear, table, stops)

    def close(self) -> None:
        self._attempt = False
        if self._loader is not None:
            self._loader.shutdown(wait=True, cancel_futures=True)
            self._loader = None
        if self._preparing is not None:
            task, self._preparing = self._preparing, None
            with contextlib.suppress(Exception):
                self.device = task.result()
        device, self.device = self.device, None
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                log.debug("preview device cleanup: %s", exc)
