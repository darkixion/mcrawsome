"""Small normalised chart model and Cairo painter shared with the browser overlay convention."""

from __future__ import annotations

import importlib
import math
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

Unit = Annotated[float, Field(ge=0, le=1)]


class ChartPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: Unit
    y: Unit | None
    low: Unit | None
    high: Unit | None


class ChartLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=64)
    colour: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    points: list[ChartPoint] = Field(max_length=256)


class ChartModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=128)
    note: str = Field(max_length=1024)
    frames: int = Field(ge=1, lt=2**31)
    lines: list[ChartLine] = Field(min_length=1, max_length=4)


def paint_chart(
    context: Any,
    model: ChartModel,
    index: int,
    width: int,
    height: int,
    *,
    phase: str = "both",
    marker: bool = True,
) -> None:
    """Only vector drawing; no per-frame image readback, encoding or graph recomputation."""
    if width < 50 or height < 50:
        return
    left, top = 12, height - min(166, max(71, height * 0.22 + 16)) - 12
    w, h = width - 24, height - top - 12
    context.save()
    if phase != "bright":
        context.rectangle(left, top, w, h)
        context.set_source_rgba(8 / 255, 17 / 255, 24 / 255, 0.84)
        context.fill_preserve()
        context.set_source_rgba(1, 1, 1, 0.12)
        context.set_line_width(1)
        context.stroke()
        context.set_source_rgba(1, 1, 1, 1)
        context.set_font_size(10)
        context.move_to(left + 10, top + 17)
        context.show_text(model.title)
        if len(model.lines) > 1:
            for i, line in enumerate(model.lines):
                colour = tuple(int(line.colour[j : j + 2], 16) / 255 for j in (1, 3, 5))
                context.set_source_rgba(*colour, 1)
                legend_x = left + w - 46 * len(model.lines) + i * 46
                context.set_line_width(1.8)
                context.set_dash(((), (8, 3), (2, 3), (8, 3, 2, 3))[i])
                context.move_to(legend_x, top + 13)
                context.line_to(legend_x + 16, top + 13)
                context.stroke()
                context.set_dash(())
                context.move_to(legend_x + 20, top + 17)
                context.show_text(line.label)
    x, y, w, h = left + 10, top + 28, w - 20, h - 36
    progress = 1.0 if model.frames == 1 else max(0.0, min(1.0, index / (model.frames - 1)))
    if phase != "bright":
        context.set_source_rgba(1, 1, 1, 0.08)
        context.set_line_width(1)
        for level in (0, 0.5, 1):
            context.move_to(x, y + level * h)
            context.line_to(x + w, y + level * h)
        context.stroke()
    for number, line in enumerate(model.lines):
        colour = tuple(int(line.colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
        for bright in (False,) if phase == "faint" else (True,) if phase == "bright" else (False, True):
            context.save()
            if bright:
                context.rectangle(x, y, w * progress, h)
                context.clip()
            if bright:
                context.set_source_rgba(*colour, 0.14)
                group: list[ChartPoint] = []

                def fill_group(points: list[ChartPoint]) -> None:
                    if len(points) < 2:
                        return
                    for i, point in enumerate(points):
                        assert point.high is not None
                        action = context.line_to if i else context.move_to
                        action(x + point.x * w, y + point.high * h)
                    for point in reversed(points):
                        assert point.low is not None
                        context.line_to(x + point.x * w, y + point.low * h)
                    context.close_path()
                    context.fill()

                for point in line.points:
                    if point.y is None or point.low is None or point.high is None:
                        fill_group(group)
                        group.clear()
                    else:
                        group.append(point)
                fill_group(group)
            context.set_source_rgba(*colour, 1 if bright else 0.22)
            context.set_line_width(1.8)
            context.set_dash(((), (8, 3), (2, 3), (8, 3, 2, 3))[number])
            connected = False
            for point in line.points:
                if point.y is None:
                    connected = False
                    continue
                if connected:
                    context.line_to(x + point.x * w, y + point.y * h)
                else:
                    context.move_to(x + point.x * w, y + point.y * h)
                connected = True
            context.stroke()
            for i, point in enumerate(line.points):
                if (
                    point.y is not None
                    and (i == 0 or line.points[i - 1].y is None)
                    and (i == len(line.points) - 1 or line.points[i + 1].y is None)
                ):
                    context.arc(x + point.x * w, y + point.y * h, 2, 0, math.tau)
                    context.fill()
            if bright:
                context.set_dash(())
                context.set_source_rgba(*colour, 0.22)
                context.set_line_width(1)
                for point in line.points:
                    if point.y is not None and point.low is not None and point.high is not None:
                        context.move_to(x + point.x * w, y + point.low * h)
                        context.line_to(x + point.x * w, y + point.high * h)
                context.stroke()
            context.restore()
    if marker:
        context.set_source_rgba(1, 1, 1, 0.8)
        context.set_line_width(1)
        context.move_to(x + progress * w, y)
        context.line_to(x + progress * w, y + h)
        context.stroke()
    context.restore()


class CachedChart:
    """Rasterise only when data/geometry changes; playback copies two small cached strips."""

    def __init__(self, model: ChartModel) -> None:
        self.model = model
        self.size = (0, 0)
        self.base: Any = None
        self.bright: Any = None

    def draw(self, context: Any, index: int, width: int, height: int) -> None:
        if width < 50 or height < 50:
            return
        band = min(166, max(71, height * 0.22 + 16))
        top = height - band - 12
        if self.size != (width, height):
            cairo = importlib.import_module("cairo")
            surfaces = []
            for phase in ("faint", "bright"):
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, math.ceil(band))
                target = cairo.Context(surface)
                target.translate(0, -top)
                paint_chart(
                    target, self.model, self.model.frames - 1, width, height, phase=phase, marker=False
                )
                surfaces.append(surface)
            self.base, self.bright = surfaces
            self.size = width, height
        progress = 1.0 if self.model.frames == 1 else max(0.0, min(1.0, index / (self.model.frames - 1)))
        x, y, w, h = 22, top + 28, width - 44, band - 36
        context.save()
        context.set_source_surface(self.base, 0, top)
        context.paint()
        context.save()
        context.rectangle(x, y, w * progress, h)
        context.clip()
        context.set_source_surface(self.bright, 0, top)
        context.paint()
        context.restore()
        context.set_source_rgba(1, 1, 1, 0.8)
        context.set_line_width(1)
        context.move_to(x + progress * w, y)
        context.line_to(x + progress * w, y + h)
        context.stroke()
        context.restore()
