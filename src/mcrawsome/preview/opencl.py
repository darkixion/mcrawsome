"""Optional OpenCL 1.2 binding using only Python's ctypes and the host GPU ICD.

No pyopencl dependency or vendor SDK is needed. Pixel maths is compiled from pixels.h,
which the native CPU preview includes too. Highlight recovery uses device kernels
at every strength, the medians of its reference ratios included, so a frame waits for
the host only at its end. Allocations are bounded by the current frame/table.
"""

from __future__ import annotations

import ctypes as ct
import logging
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.develop.raw import Developer, Image

P = ct.c_void_p
U = ct.c_uint
Int = ct.c_int
Z = ct.c_size_t
L = ct.c_uint64
log = logging.getLogger(__name__)
_runtime: ct.CDLL | None = None
MEDIAN_BINS = 2048  # compute.cl's: the widest pass of the reference medians' radix select
_MEDIAN_PASSES = ((20, 11), (9, 11), (0, 9))  # (shift, width): the 31 bits of a non-negative float
_MEDIAN_GROUP = 256  # work items in a group of the medians' scans, as compute.cl requires
_MEDIAN_ITEMS = 64 * _MEDIAN_GROUP  # each scans every 16384th reference pixel


def check(code: int) -> None:
    if code:
        raise RuntimeError(f"OpenCL error {code}")


def prepare_runtime() -> ct.CDLL:
    """Discover host drivers before a preview starts concurrent file readers.

    Some ICD constructors close a discovery descriptor twice. If a reader opens between
    those closes, its descriptor can be reused and closed by the driver. Keep the loader
    resident too; context creation and shader compilation can still run in the background.
    The worker calls this during its single-threaded startup.
    """
    global _runtime
    if _runtime is None:
        _runtime = ct.CDLL("libOpenCL.so.1")
        discover = _runtime.clGetPlatformIDs
        discover.restype, discover.argtypes = Int, [U, P, P]
        count = U()
        check(discover(0, None, ct.byref(count)))
    return _runtime


class OpenCL:
    def __init__(self) -> None:
        self.cancelled: Callable[[], bool] | None = None
        self.owned: list[tuple[str, Any]] = []
        self.buffers: dict[str, tuple[int, Any]] = {}
        self.calibration: Developer | None = None
        self.table: Image | None = None
        self.kept: Image | None = None
        self.timings: dict[str, float] = {}
        self.staged: list[npt.NDArray[Any]] = []  # host data of writes still queued (see `upload`)
        self.lib = prepare_runtime()
        declarations = {
            "clGetPlatformIDs": (Int, [U, P, P]),
            "clGetPlatformInfo": (Int, [P, U, Z, P, P]),
            "clGetDeviceIDs": (Int, [P, L, U, P, P]),
            "clGetDeviceInfo": (Int, [P, U, Z, P, P]),
            "clCreateContext": (P, [P, U, P, P, P, P]),
            "clCreateCommandQueue": (P, [P, P, L, P]),
            "clCreateProgramWithSource": (P, [P, U, P, P, P]),
            "clBuildProgram": (Int, [P, U, P, ct.c_char_p, P, P]),
            "clGetProgramBuildInfo": (Int, [P, P, U, Z, P, P]),
            "clCreateKernel": (P, [P, ct.c_char_p, P]),
            "clCreateBuffer": (P, [P, L, Z, P, P]),
            "clSetKernelArg": (Int, [P, U, Z, P]),
            "clEnqueueWriteBuffer": (Int, [P, P, U, Z, Z, P, U, P, P]),
            "clEnqueueReadBuffer": (Int, [P, P, U, Z, Z, P, U, P, P]),
            "clEnqueueNDRangeKernel": (Int, [P, P, U, P, P, P, U, P, P]),
            "clGetEventProfilingInfo": (Int, [P, U, Z, P, P]),
            "clFinish": (Int, [P]),
        }
        for name, (restype, argtypes) in declarations.items():
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = restype, argtypes
        for kind in ("Context", "CommandQueue", "Program", "Kernel", "MemObject", "Event"):
            fn = getattr(self.lib, "clRelease" + kind)
            fn.restype, fn.argtypes = Int, [P]
        self.error = Int()
        try:
            self._initialise()
        except Exception:
            self.close()
            raise

    def _initialise(self) -> None:
        count = U()
        check(self.lib.clGetPlatformIDs(0, None, ct.byref(count)))
        platforms = (P * count.value)()
        check(self.lib.clGetPlatformIDs(count, platforms, None))
        self.device = P()
        for platform in platforms:
            device = P()
            code = self.lib.clGetDeviceIDs(platform, 4, 1, ct.byref(device), None)
            if code == -1:  # CL_DEVICE_NOT_FOUND: try the next platform, never choose a CPU.
                continue
            check(code)
            if device.value:
                self.device = device
                break
        if not self.device.value:
            raise RuntimeError("no OpenCL GPU device")
        self.device_name = self.info(0x102B)
        self.driver = self.info(0x102D)
        self.context = self.own(
            "Context",
            self.lib.clCreateContext(None, 1, ct.byref(self.device), None, None, ct.byref(self.error)),
        )
        self.queue = self.own(
            "CommandQueue", self.lib.clCreateCommandQueue(self.context, self.device, 2, ct.byref(self.error))
        )
        folder = Path(__file__).parent
        code = (folder / "pixels.h").read_bytes() + b"\n" + (folder / "compute.cl").read_bytes()
        source = ct.c_char_p(code)
        self.program = self.own(
            "Program",
            self.lib.clCreateProgramWithSource(self.context, 1, ct.byref(source), None, ct.byref(self.error)),
        )
        status = self.lib.clBuildProgram(self.program, 1, ct.byref(self.device), b"-cl-std=CL1.2", None, None)
        if status:
            size = Z()
            check(self.lib.clGetProgramBuildInfo(self.program, self.device, 0x1183, 0, None, ct.byref(size)))
            message = ct.create_string_buffer(size.value)
            check(self.lib.clGetProgramBuildInfo(self.program, self.device, 0x1183, size, message, None))
            raise RuntimeError(f"OpenCL build error {status}: {message.value.decode(errors='replace')}")
        self.kernels = {
            name: self.own(
                "Kernel", self.lib.clCreateKernel(self.program, name.encode(), ct.byref(self.error))
            )
            for name in (
                "reduce",
                "transform",
                "finish",
                "highlight_flags",
                "highlight_horizontal",
                "highlight_references",
                "highlight_blur",
                "highlight_transform",
                "normalise",
                "malvar",
                "median_clear",
                "median_histogram",
                "median_select",
                "median_successor",
                "median_ratios",
            )
        }

    def info(self, key: int) -> str:
        size = Z()
        check(self.lib.clGetDeviceInfo(self.device, key, 0, None, ct.byref(size)))
        value = ct.create_string_buffer(size.value)
        check(self.lib.clGetDeviceInfo(self.device, key, size, value, None))
        return value.value.decode(errors="replace")

    def own(self, kind: str, handle: Any) -> Any:
        check(self.error.value)
        if not handle:
            raise RuntimeError(f"null OpenCL {kind}")
        self.owned.append((kind, handle))
        return handle

    def buffer(self, name: str, size: int) -> Any:
        old = self.buffers.get(name)
        if old is not None and old[0] != size:
            check(self.lib.clReleaseMemObject(old[1]))
            self.owned.remove(("MemObject", old[1]))
            del self.buffers[name]
            old = None
        if old is None:
            handle = self.own(
                "MemObject", self.lib.clCreateBuffer(self.context, 1, size, None, ct.byref(self.error))
            )
            self.buffers[name] = (size, handle)
        return self.buffers[name][1]

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise CancelledError()

    def upload(self, name: str, data: npt.NDArray[Any], *, wait: bool = True) -> Any:
        """Copy `data` to the device. Without `wait` the copy is only queued, so the device need not idle
        while the host returns to queue more; `data` is kept until a read shows the queue has run it."""
        self.check_cancelled()
        data = np.ascontiguousarray(data)
        handle = self.buffer(name, data.nbytes)
        started = time.perf_counter()
        check(
            self.lib.clEnqueueWriteBuffer(
                self.queue, handle, int(wait), 0, data.nbytes, data.ctypes.data, 0, None, None
            )
        )
        if not wait:
            self.staged.append(data)
        self.timings["upload_ms"] = self.timings.get("upload_ms", 0) + (time.perf_counter() - started) * 1000
        return handle

    def read(self, handle: Any, data: npt.NDArray[Any]) -> None:
        self.check_cancelled()
        started = time.perf_counter()
        event = P()
        try:
            check(
                self.lib.clEnqueueReadBuffer(
                    self.queue, handle, 1, 0, data.nbytes, data.ctypes.data, 0, None, ct.byref(event)
                )
            )
            self.staged.clear()  # the queue runs in order: every write before this read is done
            begin, end = L(), L()
            check(self.lib.clGetEventProfilingInfo(event, 0x1282, 8, ct.byref(begin), None))
            check(self.lib.clGetEventProfilingInfo(event, 0x1283, 8, ct.byref(end), None))
            self.timings["readback_ms"] = self.timings.get("readback_ms", 0) + (end.value - begin.value) / 1e6
        finally:
            if event.value:
                check(self.lib.clReleaseEvent(event))
        self.timings["readback_wait_ms"] = (
            self.timings.get("readback_wait_ms", 0) + (time.perf_counter() - started) * 1000
        )

    def launch(
        self, name: str, buffers: list[Any], scalars: list[int | float], width: int, height: int
    ) -> None:
        self.check_cancelled()
        kernel = self._arguments(name, buffers, scalars)
        shape = (Z * 2)(width, height)
        check(self.lib.clEnqueueNDRangeKernel(self.queue, kernel, 2, None, shape, None, 0, None, None))

    def launch_groups(
        self, name: str, buffers: list[Any], scalars: list[int | float], size: int, group: int = 0
    ) -> None:
        """`size` work items in one dimension, in work groups of `group` when the kernel needs one."""
        self.check_cancelled()
        kernel = self._arguments(name, buffers, scalars)
        size_, local = (Z * 1)(size), (Z * 1)(group) if group else None
        check(self.lib.clEnqueueNDRangeKernel(self.queue, kernel, 1, None, size_, local, 0, None, None))

    def _arguments(self, name: str, buffers: list[Any], scalars: list[int | float]) -> Any:
        kernel = self.kernels[name]
        values = [P(b) for b in buffers] + [
            ct.c_float(s) if isinstance(s, float) else Int(s) for s in scalars
        ]
        for index, value in enumerate(values):
            check(self.lib.clSetKernelArg(kernel, index, ct.sizeof(value), ct.byref(value)))
        return kernel

    def develop(self, developer: Developer, raw: npt.NDArray[np.uint16], step: int, width: int) -> Image:
        if (
            raw.ndim != 2
            or raw.shape != developer.black.shape
            or raw.shape != developer.gain.shape
            or step not in (2, 4)
            or min(raw.shape) < step
        ):
            raise ValueError("invalid preview planes, Bayer arrangement or step")
        if width < 1:
            raise ValueError("preview width must be positive")
        self.timings = {}
        self.kept = None  # an interrupted frame may overwrite the device linear buffer
        if self.calibration is not developer:
            self.upload("black", developer.black)
            self.upload("gain", developer.gain)
            self.upload("matrix", developer.matrix)
            self.calibration = developer
        source_height, source_width = raw.shape[0] // step, raw.shape[1] // step
        width = min(width, source_width)
        height = max(1, int(source_height * width / source_width + 0.5))
        rgb = np.empty((height, width, 3), np.float32)
        masks = np.empty((height, width, 3), np.uint8)
        raw_buffer = self.upload("raw", np.asarray(raw, dtype=np.uint16), wait=False)
        reduced = self.buffer("rgb", rgb.nbytes)
        mask_buffer = self.buffer("masks", masks.nbytes)
        cfa = developer.cfa
        greens = [i for i, c in enumerate(cfa) if c == "g"]
        self.launch(
            "reduce",
            [raw_buffer, self.buffers["black"][1], self.buffers["gain"][1], reduced, mask_buffer],
            [
                raw.shape[1],
                source_width,
                source_height,
                width,
                height,
                step,
                int(developer.rotate_180),
                cfa.index("r"),
                *greens,
                cfa.index("b"),
                float(developer.white),
            ],
            width,
            height,
        )
        out = np.empty_like(rgb)
        linear = self.buffer("linear", out.nbytes)
        checking = time.perf_counter()
        unclipped = raw.max() < np.float32(developer.white) - np.float32(2)
        self.timings["highlight_check_ms"] = (time.perf_counter() - checking) * 1000
        if unclipped:
            self.launch(
                "transform",
                [reduced, self.buffers["matrix"][1], linear],
                [0, 1.0, float(developer.highlight_recovery)],
                width,
                height,
            )
            self.timings["recovery_ms"] = 0.0
        else:
            self._highlights(reduced, mask_buffer, linear, width, height, developer.highlight_recovery)
        self.read(linear, out)
        self.kept = out
        return out

    def develop_full(self, developer: Developer, raw: npt.NDArray[np.uint16], stops: float = 0.0) -> Image:
        """The export's full Malvar demosaic, shading and spatial highlight recovery."""
        shape = self._develop_full(developer, raw, stops)
        out = np.empty(shape, np.float32)
        self.read(self.buffers["linear"][1], out)
        self.kept = out
        return out

    def preview_full(
        self, developer: Developer, raw: npt.NDArray[np.uint16], table: Image, stops: float
    ) -> npt.NDArray[np.uint8]:
        """Develop every photosite, keeping linear RGB on the device through the display look."""
        shape = self._develop_full(developer, raw, 0.0)
        return self._finish(shape, table, stops)

    def _develop_full(
        self, developer: Developer, raw: npt.NDArray[np.uint16], stops: float
    ) -> tuple[int, int, int]:
        if raw.ndim != 2 or raw.shape != developer.black.shape or raw.shape != developer.gain.shape:
            raise ValueError("invalid full-quality RAW planes")
        self.timings = {}
        self.kept = None
        if self.calibration is not developer:
            self.upload("black", developer.black)
            self.upload("gain", developer.gain)
            self.upload("matrix", developer.matrix)
            self.calibration = developer
        height, width = raw.shape
        raw_buffer = self.upload("raw", np.asarray(raw, np.uint16), wait=False)
        mosaic = self.buffer("mosaic", width * height * 4)
        sites = self.buffer("sites", width * height)
        rgb = self.buffer("rgb", width * height * 12)
        masks = self.buffer("masks", width * height * 3)
        flags = self.buffer("highlight_flags", width * height)
        self.launch(
            "normalise",
            [raw_buffer, self.buffers["black"][1], self.buffers["gain"][1], mosaic, sites],
            [float(developer.white)],
            width,
            height,
        )
        self.launch("malvar", [mosaic, sites, rgb, masks, flags], [developer.cfa.index("r")], width, height)
        # All spatial recovery support is within 15 pixels of a clipped photosite. Keep a
        # one-tile margin so inactive 16-pixel tiles can skip neighbourhood scans exactly.
        # Build this compact map while the queued demosaic runs, using the same float32 threshold.
        mapping = time.perf_counter()
        clipped = raw >= np.float32(developer.white) - np.float32(2)
        if not clipped.any():
            self.timings["highlight_map_ms"] = (time.perf_counter() - mapping) * 1000
            linear = self.buffer("linear", width * height * 12)
            self.launch(
                "transform",
                [rgb, self.buffers["matrix"][1], linear],
                [int(developer.rotate_180), float(2**stops), 1.0],
                width,
                height,
            )
            self.timings["recovery_ms"] = 0.0
            return height, width, 3
        if height % 16 or width % 16:
            clipped = np.pad(clipped, ((0, -height % 16), (0, -width % 16)))
        # Combine eight boolean bytes per word, then the two words in each tile row.
        words = np.ascontiguousarray(clipped).view(np.uint64).reshape((height + 15) // 16, 16, -1, 2)
        rows = np.bitwise_or.reduce(words, axis=1)
        tiles = np.asarray((rows[..., 0] | rows[..., 1]) != 0, np.uint8)
        tiles = np.asarray(cv2.dilate(tiles, np.ones((3, 3), np.uint8)), np.uint8)
        self.timings["highlight_map_ms"] = (time.perf_counter() - mapping) * 1000
        tile_buffer = self.upload("highlight_tiles", tiles, wait=False)
        linear = self.buffer("linear", width * height * 12)
        self._highlights(
            rgb,
            masks,
            linear,
            width,
            height,
            developer.highlight_recovery,
            sites=sites,
            flags=flags,
            tiles=tile_buffer,
            tile_stride=tiles.shape[1],
            rotate=developer.rotate_180,
            gain=float(2**stops),
        )
        return height, width, 3

    def _highlights(
        self,
        rgb: Any,
        masks: Any,
        linear: Any,
        width: int,
        height: int,
        strength: float,
        *,
        sites: Any = None,
        flags: Any = None,
        tiles: Any = None,
        tile_stride: int = 0,
        rotate: bool = False,
        gain: float = 1.0,
    ) -> None:
        """Keep spatial recovery on the device, the medians of the unclipped reference ring included, so
        the device never waits for the host in the middle of a frame.

        Its three exact medians retain the CPU colour contract, without sorting whole images on
        the device or reading RGB/masks back to run host morphology and recovery.
        """
        started = time.perf_counter()
        pixels = width * height
        ring = self.buffer("highlight_ring", pixels)
        white = self.buffer("highlight_white", pixels)
        seed = self.buffer("highlight_seed", pixels * 4)
        blur = self.buffer("highlight_blur", pixels * 4)
        off_seed = self.buffer("highlight_off_seed", pixels * 4)
        off_blur = self.buffer("highlight_off_blur", pixels * 4)
        values = self.buffer("highlight_values", pixels * 12)
        counter = self.buffer("highlight_count", 4)
        bins = self.buffer("median_bins", 3 * MEDIAN_BINS * 4)
        state = self.buffer("median_state", 15 * 4)
        ratio_buffer = self.buffer("highlight_ratios", 12)
        self.launch_groups("median_clear", [bins, state, counter], [], 3 * MEDIAN_BINS)
        full = sites is not None
        active = rgb if tiles is None else tiles  # stride 0 disables the tile guard for reduced frames
        radius, sigma = (12, 3.0) if full else (6, 1.5)
        key = "highlight_weights_full" if full else "highlight_weights"
        weights = (
            self.buffers[key][1]
            if key in self.buffers
            else self.upload(key, np.asarray(cv2.getGaussianKernel(2 * radius + 1, sigma), np.float32))
        )
        if flags is None:
            flags = self.buffer("highlight_flags", pixels)
            self.launch(
                "highlight_flags", [masks, sites if full else masks, flags], [int(full)], width, height
            )
        self.launch(
            "highlight_horizontal", [flags, ring, white, active], [int(full), tile_stride], width, height
        )
        self.launch(
            "highlight_references",
            [rgb, masks, ring, white, seed, off_seed, values, counter, active],
            [int(full), tile_stride],
            width,
            height,
        )
        self._medians(values, counter, bins, state, ratio_buffer)
        self.launch("highlight_blur", [seed, blur, weights, active], [radius, tile_stride], width, height)
        if strength < 1:
            self.launch(
                "highlight_blur", [off_seed, off_blur, weights, active], [radius, tile_stride], width, height
            )
        self.launch(
            "highlight_transform",
            [rgb, masks, ratio_buffer, blur, off_blur, weights, self.buffers["matrix"][1], linear, active],
            [float(strength), radius, int(rotate), gain, tile_stride],
            width,
            height,
        )
        self.timings["recovery_ms"] = (time.perf_counter() - started) * 1000

    def _medians(self, values: Any, counter: Any, bins: Any, state: Any, ratios: Any) -> None:
        """Into `ratios`, each channel's np.median of its valid (non-negative) reference ratios, or 1 without
        any: a radix select over their bits, then the successor of the lower middle value for even counts."""
        scan = (_MEDIAN_ITEMS, _MEDIAN_GROUP)
        for shift, width in _MEDIAN_PASSES:
            self.launch_groups("median_histogram", [values, counter, state, bins], [shift, width], *scan)
            first = int(shift == _MEDIAN_PASSES[0][0])
            self.launch_groups("median_select", [bins, state], [shift, width, first], 3)
        self.launch_groups("median_successor", [values, counter, state], [], *scan)
        self.launch_groups("median_ratios", [state, ratios], [], 3)

    def finish(self, linear: Image, table: Image, stops: float) -> npt.NDArray[np.uint8]:
        if linear.ndim != 3 or linear.shape[2] != 3:
            raise ValueError("the frame must be a (height, width, 3) array")
        if self.kept is not linear:
            self.upload("linear", linear)
            self.kept = linear
        return self._finish((linear.shape[0], linear.shape[1], 3), table, stops)

    def _finish(self, shape: tuple[int, int, int], table: Image, stops: float) -> npt.NDArray[np.uint8]:
        size = round(len(table) ** (1 / 3))
        if size < 2 or table.shape != (size**3, 3):
            raise ValueError("the table must be (size**3, 3) with size at least 2")
        if self.table is not table:
            self.upload("table", table)
            self.table = table
        out = np.empty(shape, np.uint8)
        output = self.buffer("output", out.nbytes)
        height, width = out.shape[:2]
        self.launch(
            "finish",
            [self.buffers["linear"][1], self.buffers["table"][1], output],
            [size, float(np.exp2(np.float32(stops)))],
            width,
            height,
        )
        self.read(output, out)
        return out

    def close(self) -> None:
        queue = getattr(self, "queue", None)
        if self.staged and self.owned and queue is not None:  # a cancelled frame's writes may still read them
            try:
                check(self.lib.clFinish(queue))
            except Exception as exc:
                log.debug("OpenCL finish: %s", exc)
        for kind, handle in reversed(self.owned):
            try:
                check(getattr(self.lib, "clRelease" + kind)(handle))
            except Exception as exc:
                log.debug("OpenCL release %s: %s", kind, exc)
        self.owned.clear()
        self.buffers.clear()
        self.staged.clear()
        self.calibration = self.table = self.kept = None
