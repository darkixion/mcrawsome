from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome.preview.chart import ChartModel, paint_chart
from mcrawsome.viewport import CropOutline, Generation, NativeViewport


def model() -> ChartModel:
    return ChartModel(
        title="Gyro · °/s",
        note="Recorded axes",
        frames=11,
        lines=[
            {
                "label": "X",
                "colour": "#68c7cb",
                "points": [
                    {"x": 0, "y": 0.5, "low": 0.4, "high": 0.6},
                    {"x": 0.5, "y": None, "low": None, "high": None},
                    {"x": 1, "y": 0.3, "low": 0.2, "high": 0.4},
                ],
            }
        ],
    )


class Context:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return lambda *args: self.calls.append((name, args))


def test_native_chart_uses_clipped_progress_and_keeps_future_lines_faint() -> None:
    chart = model()
    mid, end = Context(), Context()
    paint_chart(mid, chart, 5, 640, 360)
    paint_chart(end, chart, 10, 640, 360)
    middle_clip = [args for name, args in mid.calls if name == "rectangle"][1]
    final_clip = [args for name, args in end.calls if name == "rectangle"][1]
    assert middle_clip[2] == final_clip[2] / 2
    assert any(name == "set_source_rgba" and args[-1] == 0.22 for name, args in mid.calls)
    assert any(name == "set_source_rgba" and args[-1] == 1 for name, args in mid.calls)
    assert not any(name == "line_to" and args[0] < 22 for name, args in mid.calls)


@pytest.mark.parametrize(
    "patch",
    [
        {"frames": 0},
        {"lines": []},
        {"lines": [model().lines[0].model_dump()] * 5},
        {"lines": [{"label": "X", "colour": "bad", "points": []}]},
        {
            "lines": [
                {
                    "label": "X",
                    "colour": "#ffffff",
                    "points": [{"x": float("nan"), "y": 1, "low": None, "high": None}],
                }
            ]
        },
    ],
)
def test_native_chart_bridge_data_is_bounded(patch: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="validation error"):
        ChartModel.model_validate(model().model_dump() | patch)


def test_late_chart_changes_cannot_overwrite_a_new_mode_or_another_clip() -> None:
    host = object.__new__(NativeViewport)
    host.generation = Generation()
    host.generation.register(10)
    host.chart_revision = -1
    host.chart_area = None
    host.closed = False
    host.current = None
    host._call = lambda action: action()  # type: ignore[method-assign]
    assert host.chart(10, 2, model())
    assert not host.chart(10, 1, None)
    assert host.chart_model is not None
    assert not host.chart(9, 100, None)
    assert host.chart(10, 3, None)
    assert host.chart_model is None


def test_native_overlay_follows_the_frame_being_drawn(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcrawsome.viewport as module

    host = object.__new__(NativeViewport)
    host.chart_model = model()
    host.current = SimpleNamespace(index=7)  # type: ignore[assignment]
    calls = []
    host.chart_painter = SimpleNamespace(draw=lambda *args: calls.append(args))  # type: ignore[assignment]
    monkeypatch.setattr(module, "CachedChart", lambda *_: pytest.fail("rebuilt a chart during playback"))
    area = SimpleNamespace(get_allocated_width=lambda: 640, get_allocated_height=lambda: 360)
    context = object()
    assert host._draw_chart(area, context)
    assert calls[0][1:] == (7, 640, 360)


def test_native_chart_rasterizes_only_when_the_model_or_geometry_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcrawsome.preview.chart as module

    surfaces = []

    def surface(kind: int, width: int, height: int) -> object:
        surfaces.append((width, height))
        return object()

    cairo = SimpleNamespace(ImageSurface=surface, FORMAT_ARGB32=0, Context=lambda _: Context())
    monkeypatch.setattr("mcrawsome.preview.chart.importlib.import_module", lambda _: cairo)
    renderer = module.CachedChart(model())
    context = Context()
    for index in range(11):
        renderer.draw(context, index, 640, 360)
    assert len(surfaces) == 2
    assert all(height < 120 for _, height in surfaces), "cached full-video-sized images"
    renderer.draw(context, 5, 1280, 720)
    assert len(surfaces) == 4


def test_native_range_bands_close_at_missing_means_and_keep_single_group_whiskers() -> None:
    chart = model()
    point = chart.lines[0].points[0]
    chart.lines[0].points = [
        point,
        point.model_copy(update={"x": 0.2}),
        point.model_copy(update={"x": 0.4, "y": None}),
        point.model_copy(update={"x": 0.6}),
        point.model_copy(update={"x": 0.8}),
    ]
    context = Context()
    paint_chart(context, chart, 10, 640, 360, phase="bright", marker=False)
    assert sum(name == "close_path" for name, _ in context.calls) == 2
    assert any(name == "set_source_rgba" and args[-1] == 0.14 for name, args in context.calls)
    # Each disconnected segment starts afresh, including the original mean trace.
    assert sum(name == "move_to" for name, _ in context.calls) >= 4


def test_native_chart_draws_isolated_samples_without_connecting_a_gap() -> None:
    context = Context()
    paint_chart(context, model(), 10, 640, 360, phase="bright", marker=False)
    assert sum(name == "arc" for name, _ in context.calls) == 2


def test_native_crop_outline_cannot_be_overwritten_late_or_by_another_clip() -> None:
    host = object.__new__(NativeViewport)
    host.generation = Generation()
    host.generation.register(10)
    host.crop_revision = -1
    host.chart_area = None
    host.closed = False
    host.current = None
    host._call = lambda action: action()  # type: ignore[method-assign]
    outline = CropOutline(x=0.1, y=0.1, width=0.8, height=0.8)
    assert host.crop(10, 2, outline)
    assert not host.crop(10, 1, None)
    assert not host.crop(9, 100, None)
    assert host.crop_guide == outline
    assert host.crop(10, 3, None)
    assert host.crop_guide is None


def test_native_crop_outline_centres_the_kept_frame_on_the_fitted_picture() -> None:
    from mcrawsome.viewport import Rectangle, paint_crop_guide

    # A 16:9 picture fitted into a taller box (letterboxed above and below), drawn at device scale 2.
    rect = Rectangle(x=0, y=0, width=320, height=240, left=0, top=0, right=320, bottom=240, dpr=2)
    lease = SimpleNamespace(width=1920, height=1080)
    context = Context()
    paint_crop_guide(context, rect, lease, CropOutline(x=0.1, y=0.1, width=0.8, height=0.8), 1)  # type: ignore[arg-type]
    outline = [args for name, args in context.calls if name == "rectangle"][-1]
    # The picture is 640 x 360 device pixels from y 60; a 1.25 crop keeps 512 x 288, centred.
    assert outline == pytest.approx((64, 96, 512, 288))
    handles = Context()
    paint_crop_guide(handles, rect, lease, CropOutline(x=0, y=0, width=0.5, height=0.5, handles=True), 1)  # type: ignore[arg-type]
    squares = [args for name, args in handles.calls if name == "rectangle"][5:]
    assert len(squares) == 8
    assert (-4, 56, 8, 8) in squares  # the top-left corner of the picture


def test_native_crop_outline_follows_the_kept_frame_of_the_painted_frame() -> None:
    from mcrawsome.viewport import Rectangle, paint_crop_guide

    rect = Rectangle(x=0, y=0, width=320, height=240, left=0, top=0, right=320, bottom=240, dpr=2)
    lease = SimpleNamespace(width=1920, height=1080)
    corners = [(0.1, 0.1, 0.9, 0.1, 0.9, 0.9, 0.1, 0.9), (0.2, 0.1, 0.95, 0.15, 0.9, 0.95, 0.15, 0.9)]
    moving = CropOutline(x=0.1, y=0.1, width=0.8, height=0.8, corners=corners)
    context = Context()
    paint_crop_guide(context, rect, lease, moving, 1, 1)  # type: ignore[arg-type]
    path = [value for name, args in context.calls if name in ("move_to", "line_to") for value in args]
    # The picture is 640 x 360 device pixels from y 60. The dimming leaves frame 1's kept frame clear by
    # going round it the other way; its edge is then drawn in order.
    frame = [128, 96, 608, 114, 576, 402, 96, 384]
    assert path[:8] == pytest.approx([128, 96, 96, 384, 576, 402, 608, 114])
    assert path[8:] == pytest.approx(frame)
    past = Context()
    paint_crop_guide(past, rect, lease, moving, 1, 2)  # type: ignore[arg-type]
    assert [args for name, args in past.calls if name == "rectangle"][-1] == pytest.approx((64, 96, 512, 288))


@pytest.mark.parametrize(
    "bad",
    [
        {"x": 0.5, "y": 0, "width": 0.6, "height": 1},
        {"x": 0, "y": 0, "width": 0, "height": 1},
        {"x": float("nan"), "y": 0, "width": 1, "height": 1},
        {"x": 0, "y": 0, "width": 1, "height": 1, "corners": [[0, 0, 1, 0, 1, 1, 0]]},
        {"x": 0, "y": 0, "width": 1, "height": 1, "corners": [[0, 0, 1, 0, 1, 1, 0, 3]]},
    ],
)
def test_native_crop_outline_stays_on_the_picture(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="validation error"):
        CropOutline.model_validate(bad)
