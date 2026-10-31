"""Minimal OpenCL 1.2 C-API binding for this offline spike; pyopencl is absent here."""

from __future__ import annotations

import ctypes as ct
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import numpy as np
import numpy.typing as npt

from .common import Float, Raw

P = ct.c_void_p
U = ct.c_uint
Int = ct.c_int
Z = ct.c_size_t
L = ct.c_ulong


def check(code: int) -> None:
    if code:
        raise RuntimeError(f"OpenCL error {code}")


class Compute:
    def __init__(
        self, black: Float, gain: Float, matrix: Float, table: Float, cfa: str, rotate: bool
    ) -> None:
        self.lib = ct.CDLL("libOpenCL.so.1")
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
        self.owned: list[tuple[str, Any]] = []
        self.error = Int()
        platforms = (P * 16)()
        count = U()
        check(self.lib.clGetPlatformIDs(16, platforms, ct.byref(count)))
        self.device = P()
        for platform in platforms[: count.value]:
            platform_name = ct.create_string_buffer(1024)
            check(self.lib.clGetPlatformInfo(platform, 0x0902, 1024, platform_name, None))
            if b"Intel" in platform_name.value:
                check(self.lib.clGetDeviceIDs(platform, 4, 1, ct.byref(self.device), None))
                break
        if not self.device.value:
            raise RuntimeError("No Intel OpenCL GPU device; software fallback is not a GPU result")
        self.device_name = self.info(0x102B)
        self.driver = self.info(0x102D)
        self.context = self.own(
            "Context",
            self.lib.clCreateContext(None, 1, ct.byref(self.device), None, None, ct.byref(self.error)),
        )
        self.queue = self.own(
            "CommandQueue", self.lib.clCreateCommandQueue(self.context, self.device, 2, ct.byref(self.error))
        )
        source = ct.c_char_p(Path(__file__).with_name("develop.cl").read_bytes())
        self.program = self.own(
            "Program",
            self.lib.clCreateProgramWithSource(self.context, 1, ct.byref(source), None, ct.byref(self.error)),
        )
        code = self.lib.clBuildProgram(self.program, 1, ct.byref(self.device), b"-cl-std=CL1.2", None, None)
        if code:
            log = ct.create_string_buffer(65536)
            self.lib.clGetProgramBuildInfo(self.program, self.device, 0x1183, len(log), log, None)
            raise RuntimeError(log.value.decode())
        self.develop = self.own(
            "Kernel", self.lib.clCreateKernel(self.program, b"develop", ct.byref(self.error))
        )
        self.finish = self.own(
            "Kernel", self.lib.clCreateKernel(self.program, b"finish", ct.byref(self.error))
        )
        self.h, self.w = black.shape
        self.n = round(len(table) ** (1 / 3))
        self.rotate = rotate
        self.raw = self.buffer(np.zeros(black.shape, np.uint16))
        self.black, self.gain = self.buffer(black), self.buffer(gain)
        self.matrix, self.table = self.buffer(matrix), self.buffer(table)
        self.cfa = self.buffer(np.array(["rgb".index(c) for c in cfa], np.int32))
        self.linear = self.buffer(np.zeros((self.h, self.w, 3), np.float32))
        self.output = self.buffer(np.zeros((self.h, self.w, 3), np.uint8))

    def info(self, key: int) -> str:
        value = ct.create_string_buffer(1024)
        check(self.lib.clGetDeviceInfo(self.device, key, len(value), value, None))
        return value.value.decode()

    def own(self, kind: str, handle: Any) -> Any:
        check(self.error.value)
        if not handle:
            raise RuntimeError(f"null OpenCL {kind}")
        self.owned.append((kind, handle))
        return handle

    def buffer(self, data: npt.NDArray[Any]) -> Any:
        data = np.ascontiguousarray(data)
        return self.own(
            "MemObject",
            self.lib.clCreateBuffer(
                self.context, 1 | 32, data.nbytes, data.ctypes.data, ct.byref(self.error)
            ),
        )

    def args(self, kernel: Any, buffers: list[Any], scalars: list[int | float]) -> None:
        values = [P(b) for b in buffers] + [
            ct.c_float(s) if isinstance(s, float) else Int(s) for s in scalars
        ]
        for index, value in enumerate(values):
            check(self.lib.clSetKernelArg(kernel, index, ct.sizeof(value), ct.byref(value)))

    def launch(self, kernel: Any, width: int, height: int) -> P:
        event = P()
        shape = (Z * 2)(width, height)
        check(
            self.lib.clEnqueueNDRangeKernel(
                self.queue, kernel, 2, None, shape, None, 0, None, ct.byref(event)
            )
        )
        return event

    def duration(self, event: P) -> float:
        start, end = L(), L()
        check(self.lib.clGetEventProfilingInfo(event, 0x1282, 8, ct.byref(start), None))
        check(self.lib.clGetEventProfilingInfo(event, 0x1283, 8, ct.byref(end), None))
        check(self.lib.clReleaseEvent(event))
        return (end.value - start.value) / 1e6

    def render(
        self, raw: Raw, method: str, width: int, exposure: float
    ) -> tuple[npt.NDArray[np.uint8], dict[str, float]]:
        if raw.shape != (self.h, self.w) or raw.dtype != np.uint16 or not raw.flags.c_contiguous:
            raise ValueError("RAW shape, type or contiguity does not match calibration")
        if method not in ("quad", "bilinear"):
            raise ValueError("unknown demosaic")
        step = 2 if method == "quad" else 1
        w, h = self.w // step, self.h // step
        width = min(width, w)
        height = round(width * h / w)
        image = np.empty((height, width, 3), np.uint8)
        upload, read = P(), P()
        check(
            self.lib.clEnqueueWriteBuffer(
                self.queue, self.raw, 0, 0, raw.nbytes, raw.ctypes.data, 0, None, ct.byref(upload)
            )
        )
        self.args(
            self.develop,
            [self.raw, self.black, self.gain, self.cfa, self.matrix, self.linear],
            [self.w, self.h, int(step == 2)],
        )
        develop = self.launch(self.develop, w, h)
        self.args(
            self.finish,
            [self.linear, self.table, self.output],
            [w, h, width, height, self.n, int(self.rotate), exposure],
        )
        finish = self.launch(self.finish, width, height)
        check(
            self.lib.clEnqueueReadBuffer(
                self.queue, self.output, 1, 0, image.nbytes, image.ctypes.data, 0, None, ct.byref(read)
            )
        )
        return image, {
            "upload_ms": self.duration(upload),
            "develop_ms": self.duration(develop) + self.duration(finish),
            "readback_ms": self.duration(read),
        }

    def close(self) -> None:
        for kind, handle in reversed(self.owned):
            check(getattr(self.lib, "clRelease" + kind)(handle))
        self.owned.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
