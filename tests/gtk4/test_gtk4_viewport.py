"""The GTK 4 native preview: the GTK 3 viewport's frame copies and drawing on GTK 4's widgets, faked."""

import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from mcrawsome.gtk4.viewport import NativeViewport4
from mcrawsome.preview.chart import ChartModel
from mcrawsome.preview.display import DisplayFrames
from mcrawsome.viewport import CropOutline, NativeViewport, Rectangle

RECT = Rectangle(x=10, y=20, width=8, height=4, left=10, top=20, right=18, bottom=24, dpr=2)


class Widget:
    """A GTK 4 widget: setters are recorded by name; visibility, size and signal handlers kept as GTK does."""

    def __init__(self) -> None:
        self.set: dict[str, tuple[Any, ...]] = {}
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.visible = True  # GTK 4 widgets start visible
        self.draws = 0

    def __getattr__(self, name: str) -> Callable[..., None]:
        if not name.startswith("set_"):
            raise AttributeError(name)
        return lambda *args: self.set.__setitem__(name, args)

    def set_visible(self, visible: bool) -> None:
        self.visible = visible

    def get_visible(self) -> bool:
        return self.visible

    def connect(self, signal: str, handler: Callable[..., Any]) -> int:
        self.handlers[signal] = handler
        return 1

    def queue_draw(self) -> None:
        self.draws += 1

    def queue_resize(self) -> None:
        pass

    def get_scale_factor(self) -> int:
        return 2

    def get_width(self) -> int:
        return 640

    def get_height(self) -> int:
        return 360


class GLArea(Widget):
    """Renders at GTK's next frame (`draw_frame`), when one was queued."""

    def __init__(self) -> None:
        super().__init__()
        self.queued = False
        self.renders = 0

    def queue_render(self) -> None:
        self.queued = True

    def draw_frame(self) -> None:
        if self.queued:
            self.queued = False
            self.renders += 1
            self.handlers["render"](self, None)

    def get_error(self) -> None:
        return None


class OldGLArea(GLArea):
    def __getattr__(self, name: str) -> Callable[..., None]:
        if name == "set_allowed_apis":  # before GTK 4.12
            raise AttributeError(name)
        return super().__getattr__(name)


class Overlay:
    """The window's overlay around the web view: only overlay children may be added."""

    def __init__(self, page: object) -> None:
        self.children = [page]
        self.measured: dict[object, bool] = {}
        self.clipped: dict[object, bool] = {}

    def add_overlay(self, widget: object) -> None:
        self.children.append(widget)

    def set_measure_overlay(self, widget: object, measure: bool) -> None:
        self.measured[widget] = measure

    def set_clip_overlay(self, widget: object, clip: bool) -> None:
        self.clipped[widget] = clip


class Context:
    """Records Cairo calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *args: self.calls.append((name, args))


class Toolkit:
    """Fake `gi` modules recording the versions asked for. Idle work runs at once, or when `deferred` is
    set, waits for `run` as it would for GTK's main loop."""

    def __init__(self) -> None:
        self.required: list[tuple[str, str]] = []
        self.later: list[Callable[[], object]] = []
        self.deferred = False
        self.gtk = SimpleNamespace(GLArea=GLArea, DrawingArea=Widget, Align=SimpleNamespace(START="start"))

    def require_version(self, name: str, version: str) -> None:
        self.required.append((name, version))

    def idle_add(self, action: Callable[[], object]) -> int:
        if self.deferred:
            self.later.append(action)
        else:
            action()
        return 1

    def run(self) -> None:
        while self.later:
            self.later.pop(0)()

    def modules(self) -> dict[str, object]:
        return {
            "gi": SimpleNamespace(require_version=self.require_version),
            "gi.repository.Gtk": self.gtk,
            "gi.repository.Gdk": SimpleNamespace(GLAPI=SimpleNamespace(GL="gl")),
            "gi.repository.GLib": SimpleNamespace(idle_add=self.idle_add),
        }


@pytest.fixture
def toolkit(monkeypatch: pytest.MonkeyPatch) -> Toolkit:
    toolkit = Toolkit()
    for name, module in toolkit.modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    return toolkit


def installed(
    monkeypatch: pytest.MonkeyPatch, uploads: list[npt.NDArray[np.uint8]] | None = None
) -> NativeViewport4:
    """A GTK 4 viewport, installed and owned by page 1, with GL calls recorded instead of made."""
    host = NativeViewport4(Overlay(object()))
    recorded = [] if uploads is None else uploads
    gl = SimpleNamespace(upload=lambda pixels: recorded.append(pixels.copy()), draw=lambda *args: None)
    monkeypatch.setattr(host, "gl", gl)
    assert host.available(1)
    return host


def show(host: NativeViewport4, frames: DisplayFrames, epoch: int, index: int = 0) -> None:
    lease = frames.write(np.zeros((4, 8, 3), np.uint8), lambda: None)
    assert host.present(1, epoch, lease, RECT, index) is True
    assert frames.release(lease)
    host.area.draw_frame()


def chart_model() -> ChartModel:
    point = {"x": 0, "y": 0.5, "low": None, "high": None}
    line = {"label": "Mean", "colour": "#ffffff", "points": [point]}
    return ChartModel.model_validate({"title": "Exposure", "note": "", "frames": 10, "lines": [line]})


def test_the_gtk_4_viewport_never_asks_for_gtk_3(toolkit: Toolkit) -> None:
    host = NativeViewport4(Overlay(object()))
    assert toolkit.required == [("Gtk", "4.0"), ("Gdk", "4.0")]
    assert host.gtk is sys.modules["gi.repository.Gtk"]
    NativeViewport(SimpleNamespace())  # pywebview's window still asks for GTK 3
    assert toolkit.required[-1] == ("Gtk", "3.0")


@pytest.mark.parametrize(
    ("gl_area", "desktop_gl"),
    [(GLArea, ("set_allowed_apis", ("gl",))), (OldGLArea, ("set_use_es", (False,)))],
)
def test_install_adds_the_picture_and_chart_surfaces_over_the_page_once(
    toolkit: Toolkit, gl_area: type[GLArea], desktop_gl: tuple[str, tuple[object, ...]]
) -> None:
    toolkit.gtk.GLArea = gl_area
    page = object()
    overlay = Overlay(page)
    host = NativeViewport4(overlay)
    host.install()
    host.install()
    area, chart_area = host.area, host.chart_area
    assert overlay.children == [page, area, chart_area]  # the web view stays where the window put it
    assert area.set["set_required_version"] == (3, 3)
    name, value = desktop_gl
    assert area.set[name] == value
    assert area.set["set_auto_render"] == (False,)
    assert area.handlers == {"render": host._render, "unrealize": host._unrealise}
    assert chart_area.set["set_draw_func"] == (host._draw,)
    for widget in (area, chart_area):
        assert widget.set["set_can_target"] == (False,)  # pointer input reaches the page
        assert widget.set["set_halign"] == widget.set["set_valign"] == ("start",)
        assert not widget.visible
        assert overlay.measured[widget] is False
        assert overlay.clipped[widget] is True


def test_a_frame_is_copied_before_present_returns_and_uploaded_on_gtks_main_loop(
    toolkit: Toolkit, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[npt.NDArray[np.uint8]] = []
    host = installed(monkeypatch, uploads)
    toolkit.deferred = True
    first_image = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
    with DisplayFrames(32, slots=1) as frames:
        lease = frames.write(first_image, lambda: None)
        assert host.present(1, 0, lease, RECT, 0) is True
        assert frames.release(lease)  # the page releases the slot as soon as the reply comes
        frames.write(255 - first_image, lambda: None)  # and the engine fills it with the next frame
        toolkit.run()  # GTK's main loop runs afterwards
        host.area.draw_frame()
    [uploaded] = uploads
    np.testing.assert_array_equal(uploaded[..., :3], first_image)
    assert host.area.visible
    assert host.area.renders == 1
    assert host.area.set["set_margin_start"] == (10,)  # page pixels at device scale 2 on a scale 2 window
    assert host.area.set["set_margin_top"] == (20,)
    assert host.area.set["set_size_request"] == (8, 4)
    assert host.current is not None
    assert host.current.pixels is None  # uploaded: the copy is not kept
    assert len(host.buffers) == 1  # the copy is kept for the next frame


def test_hide_and_close_take_the_picture_away(toolkit: Toolkit, monkeypatch: pytest.MonkeyPatch) -> None:
    host = installed(monkeypatch)
    with DisplayFrames(32, slots=1) as frames:
        show(host, frames, 0)
        assert host.chart(1, 0, chart_model())
        assert host.area.visible
        assert host.chart_area.visible
        assert host.hide(1, 0)
        assert not host.area.visible
        assert not host.chart_area.visible
        assert host.current is None
        assert host.reader is None  # the slots' file is let go until the next picture
        assert host.buffers == []
        show(host, frames, 1)
        assert host.area.visible
        host.close()  # the window is closing
        host.close()
        assert host.closed
        assert not host.area.visible
        assert not host.chart_area.visible
        lease = frames.write(np.zeros((4, 8, 3), np.uint8), lambda: None)
        assert host.present(1, 2, lease, RECT, 0) is False
        assert not host.available(2)


def test_the_draw_function_paints_the_chart_and_outline_of_the_frame_drawn(
    toolkit: Toolkit, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = installed(monkeypatch)
    with DisplayFrames(32, slots=1) as frames:
        show(host, frames, 0, index=7)
    draws = host.chart_area.draws
    assert host.chart(1, 0, chart_model())
    assert host.crop(1, 0, CropOutline(x=0.1, y=0.1, width=0.8, height=0.8))
    assert host.chart_area.visible
    assert host.chart_area.draws > draws
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(host, "chart_painter", SimpleNamespace(draw=lambda *args: calls.append(args)))
    [draw] = host.chart_area.set["set_draw_func"]
    context = Context()
    draw(host.chart_area, context, 640, 360)
    assert calls == [(context, 7, 640, 360)]
    assert ("set_dash", ((6, 4),)) in context.calls  # the crop outline's edge


def test_an_outline_is_shown_before_gtk_draws_its_first_frame_and_follows_the_picture(
    toolkit: Toolkit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GTK 4 draws the chart area in the same pass as the picture and skips a surface shown during it."""
    host = installed(monkeypatch)
    assert host.crop(1, 0, CropOutline(x=0.1, y=0.1, width=0.8, height=0.8))
    assert not host.chart_area.visible  # no picture to outline yet
    [draw] = host.chart_area.set["set_draw_func"]
    with DisplayFrames(32, slots=1) as frames:
        lease = frames.write(np.zeros((4, 8, 3), np.uint8), lambda: None)
        assert host.present(1, 0, lease, RECT, 0) is True
        assert host.chart_area.visible  # before the frame is drawn
        context = Context()
        draw(host.chart_area, context, 640, 360)
        assert context.calls == []  # nothing until the picture is there
        host.area.draw_frame()
    assert host.current is not None
    draws = host.chart_area.draws
    moved = RECT.model_copy(update={"x": 6, "width": 16})  # a zoomed picture panned under the same region
    assert host.bounds(1, 0, moved)
    assert host.chart_area.draws > draws  # GTK 4 redraws only what is asked to
    assert host.area.queued


def test_shown_stabilised_each_frame_is_drawn_so_its_kept_frame_fills_the_picture(
    toolkit: Toolkit, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.viewport import kept_warp

    host = installed(monkeypatch)
    drawn: list[npt.NDArray[np.float32] | None] = []
    monkeypatch.setattr(
        host, "gl", SimpleNamespace(upload=lambda _pixels: None, draw=lambda *a: drawn.append(a[2]))
    )
    corners = [(0.1, 0.1, 0.9, 0.1, 0.9, 0.9, 0.1, 0.9), (0.2, 0.1, 0.95, 0.15, 0.9, 0.95, 0.15, 0.9)]
    outline = CropOutline(x=0.1, y=0.1, width=0.8, height=0.8, corners=corners)
    with DisplayFrames(32, slots=1) as frames:
        show(host, frames, 0, index=1)
        assert host.crop(1, 0, outline)
        host.area.draw_frame()  # outlined: drawn as recorded
        assert host.crop(1, 1, outline.model_copy(update={"stabilised": True}))
        host.area.draw_frame()  # the switch alone redraws the picture
        show(host, frames, 0, index=2)  # past the plan's frames: as recorded
    first, recorded, warped, past = drawn
    assert first is None
    assert recorded is None
    assert warped is not None
    np.testing.assert_allclose(warped, kept_warp(corners[1]))
    assert past is None
    context = Context()
    host.crop(1, 2, outline.model_copy(update={"stabilised": True}))
    [draw] = host.chart_area.set["set_draw_func"]
    draw(host.chart_area, context, 640, 360)
    assert ("set_dash", ((6, 4),)) not in context.calls  # nothing is outlined


def test_the_warp_takes_the_whole_picture_to_the_kept_frame() -> None:
    from mcrawsome.viewport import kept_warp

    corners = (0.2, 0.1, 0.95, 0.15, 0.9, 0.95, 0.15, 0.9)
    warp = kept_warp(corners).astype(np.float64)
    for (x, y), expected in zip(((0, 0), (1, 0), (1, 1), (0, 1)), np.reshape(corners, (4, 2)), strict=True):
        point = warp @ [x, y, 1.0]
        np.testing.assert_allclose(point[:2] / point[2], expected, atol=1e-6)
