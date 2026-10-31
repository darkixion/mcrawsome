"""Native viewport geometry and generation boundaries do not need a graphics driver."""

import pytest


def test_viewport_keeps_the_image_aspect_when_clipped_and_scaled() -> None:
    from mcrawsome.viewport import Rectangle

    rect = Rectangle(x=100, y=-50, width=800, height=600, left=100, top=0, right=900, bottom=550, dpr=2)
    assert rect.allocation(2) == (100, 0, 800, 550)
    # 16:9 image fits in the original 800 x 600 box, then is clipped at its top.
    assert rect.viewport(1920, 1080) == (0, 150, 1600, 900)


@pytest.mark.parametrize("patch", [{"dpr": 0}, {"width": float("inf")}, {"left": -1}, {"right": 101}])
def test_viewport_rejects_invalid_or_unbounded_rectangles(patch: dict[str, float]) -> None:
    from mcrawsome.viewport import Rectangle

    values = {
        "x": 0,
        "y": 0,
        "width": 100,
        "height": 100,
        "left": 0,
        "top": 0,
        "right": 100,
        "bottom": 100,
        "dpr": 1,
    }
    with pytest.raises(ValueError, match=r"validation error|rectangle"):
        Rectangle.model_validate(values | patch)


def test_old_clients_and_paints_cannot_cover_a_new_viewport() -> None:
    from mcrawsome.viewport import Generation

    generation = Generation()
    assert generation.register(100)
    assert generation.accept(100, 5)
    assert not generation.accept(100, 4)
    assert generation.accept(100, 6)
    assert generation.register(200)
    assert not generation.register(100)
    assert not generation.accept(100, 999)
    assert generation.accept(200, 0)


def _host(uploads: list[object], later: list[object]) -> object:
    """A viewport without GTK: idle work waits in `later`; uploads are recorded."""
    import threading
    from types import SimpleNamespace

    from mcrawsome.viewport import Generation, NativeViewport

    host = object.__new__(NativeViewport)
    host.generation = Generation()
    host.generation.register(1)
    host.pending = host.current = host.reader = None
    host.failed = host.closed = False
    host.copying, host.buffers, host.latest = threading.Lock(), [], (-1, -1, -1)
    host.chart_area = None
    host.gl = SimpleNamespace(upload=lambda pixels: uploads.append(pixels.copy()), draw=lambda *args: None)  # type: ignore[assignment]
    host.area = SimpleNamespace(show=lambda: None, hide=lambda: None, get_error=lambda: None)
    host.area.queue_render = lambda: host._render(host.area, None)
    host._position = lambda rect: None  # type: ignore[method-assign]
    host.glib = SimpleNamespace(idle_add=later.append)  # type: ignore[assignment]
    return host


def _rect() -> object:
    from mcrawsome.viewport import Rectangle

    return Rectangle(x=0, y=0, width=8, height=4, left=0, top=0, right=8, bottom=4, dpr=1)


def test_a_frame_is_copied_before_present_returns_so_its_slot_can_be_reused_at_once() -> None:
    import numpy as np

    from mcrawsome.preview.display import DisplayFrames

    uploads: list[object] = []
    later: list[object] = []
    host = _host(uploads, later)
    first_image = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
    with DisplayFrames(32, slots=1) as frames:
        lease = frames.write(first_image, lambda: None)
        assert host.present(1, 0, lease, _rect(), 0) is True  # type: ignore[attr-defined]
        assert frames.release(lease)  # the page releases the slot as soon as the reply comes
        frames.write(255 - first_image, lambda: None)  # and the engine fills it with the next frame
        for action in later:  # GTK's main loop runs afterwards
            action()  # type: ignore[operator]
    [uploaded] = uploads
    np.testing.assert_array_equal(uploaded[..., :3], first_image)  # type: ignore[index]
    assert host.current is not None  # type: ignore[attr-defined]
    assert host.current.pixels is None  # type: ignore[attr-defined]  # uploaded: the copy is not kept
    assert len(host.buffers) == 1  # type: ignore[attr-defined]  # the copy is kept for the next frame


def test_stale_and_refused_frames_are_not_copied() -> None:
    import numpy as np

    from mcrawsome.preview.display import DisplayFrames

    uploads: list[object] = []
    later: list[object] = []
    host = _host(uploads, later)
    with DisplayFrames(32, slots=2) as frames:
        lease = frames.write(np.zeros((4, 8, 3), np.uint8), lambda: None)
        assert host.present(1, 0, lease, _rect(), 5) is True  # type: ignore[attr-defined]
        assert host.present(1, 0, lease, _rect(), 5) is None  # type: ignore[attr-defined]  # not newer
        assert host.present(1, 0, lease, _rect(), 4) is None  # type: ignore[attr-defined]
        assert host.present(2, 0, lease, _rect(), 6) is False  # type: ignore[attr-defined]  # not the owner
        for action in later:
            action()  # type: ignore[operator]
        assert len(uploads) == 1
        host.closed = True  # type: ignore[attr-defined]
        assert host.present(1, 1, lease, _rect(), 6) is False  # type: ignore[attr-defined]


def test_the_web_view_moves_under_the_preview_overlay_once() -> None:
    """Moving the web view redraws the page; it happens before the window is shown, never on first use."""
    import threading
    from types import SimpleNamespace

    from mcrawsome.viewport import Generation, NativeViewport

    moves: list[str] = []

    class Widget:
        def __init__(self, *args: object) -> None:
            self.children: list[object] = []

        def __getattr__(self, name: str) -> object:
            return lambda *args: None

        def add(self, child: object) -> None:
            self.children.append(child)

        def add_overlay(self, child: object) -> None:
            self.children.append(child)

    class Native(Widget):
        def get_child(self) -> object:
            return self.children[-1]

        def remove(self, child: object) -> None:
            moves.append("remove")
            self.children.remove(child)

    native, page = Native(), object()
    native.add(page)
    host = object.__new__(NativeViewport)
    host.window = SimpleNamespace(native=native)
    gtk = SimpleNamespace(Overlay=Widget, GLArea=Widget, DrawingArea=Widget, Align=SimpleNamespace(START=0))
    host.gtk, host.glib = gtk, SimpleNamespace(idle_add=lambda action: action())  # type: ignore[assignment]
    host.generation = Generation()
    host.area = host.chart_area = host.current = host.pending = host.reader = None
    host.chart_model, host.chart_revision, host.chart_painter = None, -1, None
    host.failed = host.closed = False
    host.copying, host.buffers, host.latest = threading.Lock(), [], (-1, -1, -1)
    host.install()
    overlay = native.get_child()
    assert isinstance(overlay, Widget)
    assert overlay.children[0] is page
    assert host.available(1)
    host.install()
    assert moves == ["remove"]
