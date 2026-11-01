"""GTK presentation of local display leases, with no OpenCL/GL context sharing.

A frame is copied out of its slot on the bridge's thread, so the engine gets the slot back at once and
GTK's main loop, which the page also needs, only uploads and draws it."""

from __future__ import annotations

import ctypes as ct
import importlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Self

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mcrawsome.preview.chart import CachedChart, ChartModel
from mcrawsome.preview.display import DisplayLease, DisplayReader


class Rectangle(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x: float = Field(ge=-32768, le=32768)
    y: float = Field(ge=-32768, le=32768)
    width: float = Field(gt=0, le=32768)
    height: float = Field(gt=0, le=32768)
    left: float = Field(ge=0, le=32768)
    top: float = Field(ge=0, le=32768)
    right: float = Field(gt=0, le=32768)
    bottom: float = Field(gt=0, le=32768)
    dpr: float = Field(gt=0, le=8)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if not (self.x <= self.left < self.right <= self.x + self.width + 0.01):
            raise ValueError("invalid horizontal rectangle")
        if not (self.y <= self.top < self.bottom <= self.y + self.height + 0.01):
            raise ValueError("invalid vertical rectangle")
        return self

    def allocation(self, scale: int) -> tuple[int, int, int, int]:
        ratio = self.dpr / scale
        x, y = math.floor(self.left * ratio), math.floor(self.top * ratio)
        return x, y, math.ceil(self.right * ratio) - x, math.ceil(self.bottom * ratio) - y

    def viewport(self, width: int, height: int) -> tuple[int, int, int, int]:
        ratio = min(self.width / width, self.height / height)
        w, h = width * ratio, height * ratio
        x = self.x + (self.width - w) / 2 - self.left
        y = self.bottom - (self.y + (self.height + h) / 2)
        return tuple(round(value * self.dpr) for value in (x, y, w, h))  # type: ignore[return-value]


class Generation:
    """Only the newest mounted page and its current request may show pixels."""

    def __init__(self) -> None:
        self.owner = -1
        self.epoch = -1

    def register(self, owner: int) -> bool:
        if owner < self.owner:
            return False
        if owner > self.owner:
            self.owner, self.epoch = owner, -1
        return True

    def accept(self, owner: int, epoch: int) -> bool:
        if owner != self.owner or epoch < self.epoch:
            return False
        self.epoch = epoch
        return True


class _GL:
    """Small desktop OpenGL interface, called only with the GLArea context current."""

    def __init__(self) -> None:
        self.lib = ct.CDLL("libGL.so.1")
        u, i, p = ct.c_uint, ct.c_int, ct.c_void_p
        signatures: dict[str, tuple[Any, list[Any]]] = {
            "GenTextures": (None, [i, p]),
            "BindTexture": (None, [u, u]),
            "TexImage2D": (None, [u, i, i, i, i, i, u, u, p]),
            "TexSubImage2D": (None, [u, i, i, i, i, i, u, u, p]),
            "TexParameteri": (None, [u, u, i]),
            "ActiveTexture": (None, [u]),
            "CreateShader": (u, [u]),
            "ShaderSource": (None, [u, i, p, p]),
            "CompileShader": (None, [u]),
            "GetShaderiv": (None, [u, u, p]),
            "CreateProgram": (u, []),
            "AttachShader": (None, [u, u]),
            "LinkProgram": (None, [u]),
            "GetProgramiv": (None, [u, u, p]),
            "UseProgram": (None, [u]),
            "DeleteShader": (None, [u]),
            "DeleteProgram": (None, [u]),
            "GenVertexArrays": (None, [i, p]),
            "BindVertexArray": (None, [u]),
            "DeleteVertexArrays": (None, [i, p]),
            "DeleteTextures": (None, [i, p]),
            "GetUniformLocation": (i, [u, ct.c_char_p]),
            "Uniform1i": (None, [i, i]),
            "Uniform1f": (None, [i, ct.c_float]),
            "UniformMatrix3fv": (None, [i, i, ct.c_ubyte, p]),
            "DrawArrays": (None, [u, i, i]),
            "Viewport": (None, [i, i, i, i]),
            "PixelStorei": (None, [u, i]),
            "BindBuffer": (None, [u, u]),
            "Disable": (None, [u]),
            "ClearColor": (None, [ct.c_float] * 4),
            "Clear": (None, [u]),
            "Finish": (None, []),
            "GetError": (u, []),
        }
        self.functions: dict[str, Any] = {}
        for name, (result, arguments) in signatures.items():
            function = getattr(self.lib, "gl" + name)
            function.restype, function.argtypes = result, arguments
            self.functions[name] = function
        self.program = int(self.call("CreateProgram"))
        self.texture = u()
        self.vao = u()
        self.size = (0, 0, 0)  # width, height and bytes per pixel of the texture
        try:
            sources = (
                (
                    0x8B31,
                    b"#version 330 core\nout vec2 uv; void main(){vec2 p=vec2((gl_VertexID<<1)&2,"
                    b"gl_VertexID&2);uv=p;gl_Position=vec4(p*2.0-1.0,0,1);}",
                ),
                (  # `warp` takes each point of the picture to where it is drawn from (`kept_warp`)
                    0x8B30,
                    b"#version 330 core\nin vec2 uv; out vec4 colour; uniform sampler2D frame;"
                    b"uniform mat3 warp; void main(){vec3 s=warp*vec3(uv.x,1.0-uv.y,1.0);"
                    b"colour=vec4(texture(frame,s.xy/s.z).rgb,1.0);}",
                ),
            )
            for kind, source in sources:
                shader = int(self.call("CreateShader", kind))
                try:
                    text = ct.c_char_p(source)
                    self.call("ShaderSource", shader, 1, ct.byref(text), None)
                    self.call("CompileShader", shader)
                    ok = i()
                    self.call("GetShaderiv", shader, 0x8B81, ct.byref(ok))
                    if not ok.value:
                        raise RuntimeError("native preview shader compilation failed")
                    self.call("AttachShader", self.program, shader)
                finally:
                    self.call("DeleteShader", shader)
            self.call("LinkProgram", self.program)
            ok = i()
            self.call("GetProgramiv", self.program, 0x8B82, ct.byref(ok))
            if not ok.value:
                raise RuntimeError("native preview shader linking failed")
            self.call("GenTextures", 1, ct.byref(self.texture))
            self.call("GenVertexArrays", 1, ct.byref(self.vao))
            self.call("BindTexture", 0x0DE1, self.texture.value)
            for key, value in ((0x2801, 0x2601), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
                self.call("TexParameteri", 0x0DE1, key, value)
            self.check()
        except BaseException:
            self.close()
            raise

    def call(self, name: str, *args: Any) -> Any:
        return self.functions[name](*args)

    def check(self) -> None:
        if self.call("GetError"):
            raise RuntimeError("native preview OpenGL operation failed")

    def upload(self, pixels: npt.NDArray[np.uint8]) -> None:
        """BGRX (as BGRA, which drivers take without converting each pixel) or an older engine's BGR."""
        height, width, channels = pixels.shape
        internal, layout, kind = (0x8058, 0x80E1, 0x8367) if channels == 4 else (0x8051, 0x80E0, 0x1401)
        self.call("BindBuffer", 0x88EC, 0)
        self.call("PixelStorei", 0x0CF5, 1)
        self.call("ActiveTexture", 0x84C0)
        self.call("BindTexture", 0x0DE1, self.texture.value)
        if self.size != (width, height, channels):
            self.call("TexImage2D", 0x0DE1, 0, internal, width, height, 0, layout, kind, None)
            self.size = (width, height, channels)
        self.call(
            "TexSubImage2D", 0x0DE1, 0, 0, 0, width, height, layout, kind, ct.c_void_p(pixels.ctypes.data)
        )

    def draw(self, lease: DisplayLease, rect: Rectangle, warp: npt.NDArray[np.float32] | None = None) -> None:
        """`warp` (3 x 3, row by row) maps each point of the drawn picture to the point of the frame shown
        there, both as fractions from the top left; None draws the frame as it is."""
        self.call("Disable", 0x8DB9)  # display codes are already transfer encoded
        self.call("Disable", 0x0BE2)
        self.call("ClearColor", 8 / 255, 11 / 255, 13 / 255, 1)
        self.call("Clear", 0x4000)
        self.call("Viewport", *rect.viewport(lease.width, lease.height))
        self.call("UseProgram", self.program)
        self.call("Uniform1i", self.call("GetUniformLocation", self.program, b"frame"), 0)
        matrix = np.ascontiguousarray(np.eye(3, dtype=np.float32) if warp is None else warp, dtype=np.float32)
        location = self.call("GetUniformLocation", self.program, b"warp")
        self.call("UniformMatrix3fv", location, 1, 1, ct.c_void_p(matrix.ctypes.data))  # 1: row by row
        self.call("BindVertexArray", self.vao.value)
        self.call("DrawArrays", 4, 0, 3)
        self.call("Finish")
        self.check()

    def close(self) -> None:
        self.call("DeleteTextures", 1, ct.byref(self.texture))
        self.call("DeleteVertexArrays", 1, ct.byref(self.vao))
        self.call("DeleteProgram", self.program)


Fraction = Annotated[float, Field(ge=0, le=1)]
Spot = Annotated[float, Field(ge=-1, le=2)]  # a moving kept frame may reach a little past the picture
Corners = tuple[Spot, Spot, Spot, Spot, Spot, Spot, Spot, Spot]


class CropOutline(BaseModel):
    """A rectangle on the shown picture, as fractions of its width and height: the frame a stabilised
    timeline keeps, or a mounted DNG crop being placed (with `handles` to resize it). `corners` moves the
    kept frame as the stabilisation will: for each frame, x and y of its top left, top right, bottom
    right and bottom left (`resolve.kept_frame`); frames past them keep the rectangle. `stabilised` shows
    the picture as the stabilisation will instead: each frame's kept frame fills it, and nothing is
    outlined."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: Fraction
    y: Fraction
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]
    handles: bool = False
    corners: list[Corners] | None = Field(None, max_length=1_000_000)
    stabilised: bool = False

    @model_validator(mode="after")
    def inside(self) -> Self:
        if self.x + self.width > 1.0001 or self.y + self.height > 1.0001:
            raise ValueError("the outline leaves the picture")
        return self


def kept_warp(corners: Corners) -> npt.NDArray[np.float32]:
    """The projective map (3 x 3, row by row) from the whole picture to a frame's kept frame, both as
    fractions from the top left: drawn through it, the kept frame fills the picture."""
    square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    rows, values = [], []
    for (x, y), (u, v) in zip(square, zip(corners[0::2], corners[1::2], strict=True), strict=True):
        rows += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
        values += [u, v]
    solved = np.linalg.solve(np.array(rows, dtype=np.float64), np.array(values, dtype=np.float64))
    return np.append(solved, 1.0).reshape(3, 3).astype(np.float32)


def paint_crop_guide(
    context: Any, rect: Rectangle, lease: DisplayLease, outline: CropOutline, scale: int, index: int = 0
) -> None:
    """Dim the picture outside `outline` and draw its edge, in the overlay's coordinates, where it lies on
    frame `index`. The picture is fitted into `rect` as `_GL.draw` fits it."""
    ratio = rect.dpr / scale
    fit = min(rect.width / lease.width, rect.height / lease.height)
    pw, ph = lease.width * fit * ratio, lease.height * fit * ratio
    px = (rect.x + rect.width / 2) * ratio - pw / 2 - math.floor(rect.left * ratio)
    py = (rect.y + rect.height / 2) * ratio - ph / 2 - math.floor(rect.top * ratio)
    kx, ky, kw, kh = px + outline.x * pw, py + outline.y * ph, outline.width * pw, outline.height * ph
    moving = outline.corners[index] if outline.corners and 0 <= index < len(outline.corners) else None
    points = [(px + moving[i] * pw, py + moving[i + 1] * ph) for i in range(0, 8, 2)] if moving else []
    context.save()
    context.set_source_rgba(0, 0, 0, 0.45)
    if points:
        context.save()
        context.rectangle(px, py, pw, ph)
        context.clip()  # where the kept frame reaches past the picture, nothing is dimmed
        context.rectangle(px, py, pw, ph)
        context.move_to(*points[0])  # wound the other way round, the kept frame is left clear
        for point in points[:0:-1]:
            context.line_to(*point)
        context.close_path()
        context.fill()
        context.restore()
    else:
        for x, y, w, h in (
            (px, py, pw, ky - py),
            (px, ky + kh, pw, py + ph - ky - kh),
            (px, ky, kx - px, kh),
            (kx + kw, ky, px + pw - kx - kw, kh),
        ):
            context.rectangle(x, y, w, h)
        context.fill()
    context.set_source_rgba(1, 1, 1, 0.8)
    context.set_line_width(1.5)
    context.set_dash((6, 4))
    if points:
        context.move_to(*points[0])
        for point in points[1:]:
            context.line_to(*point)
        context.close_path()
    else:
        context.rectangle(kx, ky, kw, kh)
    context.stroke()
    if outline.handles:
        context.set_dash(())
        context.set_source_rgba(1, 1, 1, 1)
        for hx in (kx, kx + kw / 2, kx + kw):
            for hy in (ky, ky + kh / 2, ky + kh):
                if (hx, hy) != (kx + kw / 2, ky + kh / 2):
                    context.rectangle(hx - 4, hy - 4, 8, 8)
        context.fill()
    context.restore()


@dataclass
class _Paint:
    owner: int
    epoch: int
    lease: DisplayLease
    rect: Rectangle
    index: int = 0
    pixels: npt.NDArray[np.uint8] | None = field(default=None, repr=False)  # until uploaded


_KEPT_BUFFERS = 3  # frame copies kept for reuse; more than this are given back to the system


class NativeViewport:
    """The bridge copies each frame and returns; all widget and GL work stays on GTK's main loop."""

    # pywebview's window. GTK 3 and 4 cannot share a process: the GTK 4 window's subclass asks for 4.0.
    gtk_version = "3.0"

    def __init__(self, window: Any) -> None:
        gi = importlib.import_module("gi")
        gi.require_version("Gtk", self.gtk_version)
        self.gtk = importlib.import_module("gi.repository.Gtk")
        self.glib = importlib.import_module("gi.repository.GLib")
        self.window = window
        self.generation = Generation()
        self.area: Any = None
        self.chart_area: Any = None
        self.chart_model: ChartModel | None = None
        self.chart_revision = -1
        self.chart_painter: CachedChart | None = None
        self.crop_guide: CropOutline | None = None
        self.crop_revision = -1
        self.gl: _GL | None = None
        self.reader: DisplayReader | None = None
        self.pending: _Paint | None = None
        self.current: _Paint | None = None
        # The bridge's threads copy frames under this lock; the reader and the buffer pool belong to it.
        self.copying = threading.Lock()
        self.buffers: list[npt.NDArray[np.uint8]] = []
        self.latest = (-1, -1, -1)  # owner, epoch and index of the newest frame copied
        self.failed = False
        self.closed = False

    def _call(self, action: Callable[[], bool]) -> bool:
        done = threading.Event()
        result = False

        def run() -> bool:
            nonlocal result
            if not done.is_set():
                try:
                    result = action()
                except (OSError, ValueError, RuntimeError, AttributeError):
                    result = False
                finally:
                    done.set()
            return False

        self.glib.idle_add(run)
        if not done.wait(2):
            done.set()
            return False
        return result

    def install(self) -> None:
        """Put the web view under an overlay for the picture and chart surfaces, once, on GTK's main loop.
        Done before the window is shown (`before_show`): moving the web view later redraws the whole page,
        which flickered when the first clip was selected."""
        if self.area is not None:
            return
        native = self.window.native
        child = native.get_child()
        native.remove(child)
        overlay = self.gtk.Overlay()
        overlay.add(child)
        native.add(overlay)
        area = self.gtk.GLArea()
        area.set_required_version(3, 3)
        area.set_use_es(False)
        area.set_auto_render(False)
        area.set_halign(self.gtk.Align.START)
        area.set_valign(self.gtk.Align.START)
        area.set_no_show_all(True)
        area.connect("render", self._render)
        area.connect("unrealize", self._unrealise)
        overlay.add_overlay(area)
        overlay.set_overlay_pass_through(area, True)
        self.area = area
        chart_area = self.gtk.DrawingArea()
        chart_area.set_halign(self.gtk.Align.START)
        chart_area.set_valign(self.gtk.Align.START)
        chart_area.set_no_show_all(True)
        chart_area.set_app_paintable(True)
        chart_area.connect("draw", self._draw_chart)
        overlay.add_overlay(chart_area)
        overlay.set_overlay_pass_through(chart_area, True)
        self.chart_area = chart_area
        native.connect("destroy", self._destroy)
        overlay.show_all()

    def available(self, owner: int) -> bool:
        def start() -> bool:
            previous_owner = self.generation.owner
            if self.closed or self.failed or not self.generation.register(owner):
                return False
            if previous_owner != owner:
                self.chart_model, self.chart_revision, self.chart_painter = None, -1, None
                self.crop_guide, self.crop_revision = None, -1
            self.install()
            self._hide()
            return True

        return self._call(start)

    def present(
        self, owner: int, epoch: int, lease: DisplayLease, rect: Rectangle, index: int = 0
    ) -> bool | None:
        """Copy the frame out of its slot and queue it for the next render. False when this viewport cannot
        show it (closed, failed or taken by a newer page: the page falls back to JPEG); None when a newer
        frame of the same request was already copied. Once this returns, the slot may be released."""
        if (
            self.closed
            or self.failed
            or self.area is None
            or owner != self.generation.owner
            or epoch < self.generation.epoch
        ):
            return False
        with self.copying:
            if (owner, epoch) == self.latest[:2] and index <= self.latest[2]:
                return None
            self.latest = (owner, epoch, index)
            try:
                if self.reader is None or self.reader.lease.name != lease.name:
                    if self.reader is not None:
                        self.reader.close()
                        self.reader = None
                    self.reader = DisplayReader(lease)
                source = self.reader.pixels(lease)
                pixels = self._buffer(source.shape)
                np.copyto(pixels, source)
                del source
            except (OSError, ValueError):
                return False
        paint = _Paint(owner, epoch, lease, rect, index=index, pixels=pixels)
        self.glib.idle_add(lambda: self._queue(paint))
        return True

    def _buffer(self, shape: tuple[int, ...]) -> npt.NDArray[np.uint8]:
        """A frame copy from the pool (under `copying`), or a new one."""
        while self.buffers:
            buffer = self.buffers.pop()
            if buffer.shape == shape:
                return buffer
        return np.empty(shape, np.uint8)

    def _give_back(self, paint: _Paint | None) -> None:
        """Return a paint's frame copy to the pool once nothing will upload it."""
        if paint is None or paint.pixels is None:
            return
        pixels, paint.pixels = paint.pixels, None
        with self.copying:
            if len(self.buffers) < _KEPT_BUFFERS:
                self.buffers.append(pixels)

    def _queue(self, paint: _Paint) -> bool:
        """On the main loop: make the copied frame the next one drawn, unless a newer one is waiting."""
        newest = self.pending or self.current
        if not self._accept(paint.owner, paint.epoch) or (
            newest is not None and newest.epoch == paint.epoch and newest.index >= paint.index
        ):
            self._give_back(paint)
            return False
        self._give_back(self.pending)  # a frame not drawn in time: the newest wins
        self.pending = paint
        self._position(paint.rect)
        self._set_visible(self.area, True)
        self.area.queue_render()
        return False

    def bounds(self, owner: int, epoch: int, rect: Rectangle) -> bool:
        def move() -> bool:
            if not self._accept(owner, epoch) or self.current is None:
                return False
            self.current.rect = rect
            self._position(rect)
            self.area.queue_render()
            return True

        return self._call(move)

    def hide(self, owner: int, epoch: int) -> bool:
        def hide() -> bool:
            if not self.generation.accept(owner, epoch):
                return False
            self._hide()
            return True

        return self._call(hide)

    def chart(self, owner: int, revision: int, model: ChartModel | None) -> bool:
        def change() -> bool:
            if self.closed or owner != self.generation.owner or revision < self.chart_revision:
                return False
            self.chart_revision, self.chart_model = revision, model
            self.chart_painter = CachedChart(model) if model is not None else None
            self._update_chart()
            return True

        return self._call(change)

    def crop(self, owner: int, revision: int, crop: CropOutline | None) -> bool:
        """Outline part of the picture (`CropOutline`); None removes the outline."""

        def change() -> bool:
            if self.closed or owner != self.generation.owner or revision < self.crop_revision:
                return False
            self.crop_revision, self.crop_guide = revision, crop
            self._update_chart()
            area = getattr(self, "area", None)
            if area is not None and self.current is not None:  # the picture may now be warped
                area.queue_render()
            return True

        return self._call(change)

    def _update_chart(self) -> None:
        area = getattr(self, "chart_area", None)
        if area is None:
            return
        if (self.chart_model is not None or getattr(self, "crop_guide", None)) and self.current is not None:
            self._set_visible(area, True)
            area.queue_draw()
        else:
            self._set_visible(area, False)

    def _draw_chart(self, area: Any, context: Any) -> bool:
        crop: CropOutline | None = getattr(self, "crop_guide", None)
        shown = self.current
        if crop and not crop.stabilised and shown is not None:
            paint_crop_guide(context, shown.rect, shown.lease, crop, area.get_scale_factor(), shown.index)
        if self.chart_model is not None and self.current is not None:
            if self.chart_painter is None:
                self.chart_painter = CachedChart(self.chart_model)
            self.chart_painter.draw(context, self.current.index, *self._size(area))
        return True

    def _set_visible(self, widget: Any, visible: bool) -> None:
        """Show or hide a surface (GTK 4 deprecates `show` and `hide` for `set_visible`)."""
        if visible:
            widget.show()
        else:
            widget.hide()

    def _size(self, area: Any) -> tuple[int, int]:
        """A surface's width and height (`get_width` and `get_height` on GTK 4)."""
        return area.get_allocated_width(), area.get_allocated_height()

    def _accept(self, owner: int, epoch: int) -> bool:
        return (
            not self.closed
            and not self.failed
            and self.area is not None
            and self.generation.accept(owner, epoch)
        )

    def _position(self, rect: Rectangle) -> None:
        x, y, width, height = rect.allocation(self.area.get_scale_factor())
        for surface in (self.area, getattr(self, "chart_area", None)):
            if surface is not None:
                surface.set_margin_start(x)
                surface.set_margin_top(y)
                surface.set_size_request(width, height)
                if not surface.get_visible():
                    # GTK measures a hidden widget again only when asked: it would come back at its old
                    # size, with the picture drawn for this one shifted inside it.
                    surface.queue_resize()

    def _hide(self) -> None:
        self._give_back(self.pending)
        self.pending = None
        self.current = None
        self._update_chart()
        if self.area is not None:
            self._set_visible(self.area, False)
        with self.copying:  # the slots' file and the frame copies are not needed until the next picture
            if self.reader is not None:
                self.reader.close()
                self.reader = None
            self.buffers.clear()
            self.latest = (-1, -1, -1)

    def _render(self, area: Any, context: Any) -> bool:
        paint = self.pending or self.current
        if paint is None:
            return True
        try:
            if area.get_error() is not None:
                raise RuntimeError(str(area.get_error()))
            if self.gl is None:
                self.gl = _GL()
            if paint is self.pending:
                assert paint.pixels is not None
                self.gl.upload(paint.pixels)
                self._give_back(paint)
                self.current, self.pending = paint, None
                self._update_chart()
            self.gl.draw(paint.lease, paint.rect, self._warp(paint.index))
        except (OSError, ValueError, RuntimeError, AttributeError) as error:
            import logging

            logging.getLogger(__name__).warning("Using JPEG preview: %s", error)
            self.failed = True
            self._hide()
        return True

    def _warp(self, index: int) -> npt.NDArray[np.float32] | None:
        """How frame `index` is drawn when the picture is shown stabilised; None draws it as recorded."""
        crop: CropOutline | None = getattr(self, "crop_guide", None)
        if crop is None or not crop.stabilised or not crop.corners or not 0 <= index < len(crop.corners):
            return None
        return kept_warp(crop.corners[index])

    def _unrealise(self, area: Any) -> None:
        self._hide()
        if self.gl is not None:
            area.make_current()
            if area.get_error() is None:
                self.gl.close()
            self.gl = None

    def close(self) -> None:
        """On GTK's main loop as the window closes: no more pictures, and the slots' file is let go."""
        self.closed = True
        self._hide()

    def _destroy(self, window: Any) -> None:
        self.close()
