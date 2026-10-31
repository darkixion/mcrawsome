"""The native preview on GTK 4: `mcrawsome.viewport`'s frame copies, GL drawing, charts and crop outlines, on
surfaces added to the overlay the GTK 4 window builds around its web view. GTK composites them on the GPU,
so a new frame redraws only the picture and, when one is shown, the chart."""

from __future__ import annotations

import importlib
from typing import Any

from mcrawsome.viewport import NativeViewport, Rectangle


class NativeViewport4(NativeViewport):
    """Built from the window's `Gtk.Overlay`; the bridge calls it as it calls the GTK 3 viewport."""

    gtk_version = "4.0"

    def __init__(self, overlay: Any) -> None:
        super().__init__(overlay)
        importlib.import_module("gi").require_version("Gdk", "4.0")
        self.gdk = importlib.import_module("gi.repository.Gdk")
        self.overlay = overlay

    def install(self) -> None:
        """Add the picture and chart surfaces over the web view, once, on GTK's main loop. The web view is
        already in the overlay, so nothing is moved and the page is not redrawn."""
        if self.area is not None:
            return
        gtk = self.gtk
        area = gtk.GLArea()
        area.set_required_version(3, 3)
        if hasattr(area, "set_allowed_apis"):  # GTK 4.12: desktop GL, as the shaders are GL 3.3 core
            area.set_allowed_apis(self.gdk.GLAPI.GL)
        else:
            area.set_use_es(False)
        area.set_auto_render(False)
        area.connect("render", self._render)
        area.connect("unrealize", self._unrealise)
        chart_area = gtk.DrawingArea()
        chart_area.set_draw_func(self._draw)
        for widget in (area, chart_area):
            widget.set_can_target(False)  # pointer input reaches the page underneath
            widget.set_halign(gtk.Align.START)  # placed by margins and sized by `_position`
            widget.set_valign(gtk.Align.START)
            widget.set_visible(False)
            self.overlay.add_overlay(widget)
            self.overlay.set_measure_overlay(widget, False)  # the page alone sizes the window
            self.overlay.set_clip_overlay(widget, True)
        self.area, self.chart_area = area, chart_area

    def _position(self, rect: Rectangle) -> None:
        """GTK 3 repaints the whole window; GTK 4 redraws only what is asked, so the chart and crop outline
        are asked to follow the picture here too."""
        super()._position(rect)
        self._update_chart()

    def _update_chart(self) -> None:
        """Shown as soon as a frame waits to be drawn, not once it is: GTK 4 draws the chart in the same pass
        as the picture (`_render`), and a surface shown during that pass is skipped until another redraw."""
        if self.chart_area is None:
            return
        shown = (self.chart_model is not None or self.crop_guide is not None) and (
            self.pending is not None or self.current is not None
        )
        self.chart_area.set_visible(shown)
        if shown:
            self.chart_area.queue_draw()

    def _set_visible(self, widget: Any, visible: bool) -> None:
        widget.set_visible(visible)

    def _size(self, area: Any) -> tuple[int, int]:
        return area.get_width(), area.get_height()

    def _draw(self, area: Any, context: Any, width: int, height: int) -> None:
        """The chart area's draw function (GTK 4 has no "draw" signal); nothing until a frame is drawn."""
        self._draw_chart(area, context)
