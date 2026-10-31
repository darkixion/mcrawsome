"""Forward native desktop drops as file references; never read or transfer clip contents."""

import importlib
import json
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlsplit


def _forward_paths(window: Any, paths: list[str]) -> None:
    window.evaluate_js(
        "window.dispatchEvent(new CustomEvent('mcrawsome-files-dropped', {detail: "
        + json.dumps(paths)
        + "}));"
    )


def forward_drop(window: Any, event: dict[str, Any]) -> None:
    files = event.get("dataTransfer", {}).get("files", [])
    paths = [entry["pywebviewFullPath"] for entry in files if isinstance(entry.get("pywebviewFullPath"), str)]
    if paths:
        _forward_paths(window, paths)


class GtkFileDrop:
    """Consume URI data requested for an actual drop, never WebKit's hover requests."""

    def __init__(self, deliver: Callable[[list[str]], None], finish: Callable[..., Any]) -> None:
        self.deliver = deliver
        self.finish = finish
        self.pending: Any = None

    def drop(self, widget: Any, context: Any, x: int, y: int, stamp: int) -> bool:
        target = next((atom for atom in context.list_targets() if str(atom) == "text/uri-list"), None)
        if target is None:
            return False
        self.pending = context
        widget.drag_get_data(context, target, stamp)
        return True

    def received(self, widget: Any, context: Any, x: int, y: int, data: Any, info: int, stamp: int) -> None:
        if self.pending is None or context != self.pending or str(data.get_target()) != "text/uri-list":
            return
        self.pending = None
        widget.stop_emission_by_name("drag-data-received")
        paths = []
        for uri in data.get_uris() or []:
            try:
                parsed = urlsplit(uri)
            except ValueError:
                continue
            path = unquote(parsed.path)
            if (
                parsed.scheme == "file"
                and parsed.netloc in {"", "localhost"}
                and path.startswith("/")
                and "\0" not in path
            ):
                paths.append(path)
        self.finish(context, bool(paths), False, stamp)
        if paths:
            self.deliver(paths)


def attach_file_drop(window: Any) -> None:
    """Attach once during before_show, on GTK's main thread."""
    if window.gui.renderer == "gtkwebkit2":
        gtk = importlib.import_module("gi.repository.Gtk")
        view = window.gui.BrowserView.instances[window.uid].webview

        def deliver(paths: list[str]) -> None:
            # evaluate_js waits for WebKit's main loop, so cannot run on that loop.
            threading.Thread(target=_forward_paths, args=(window, paths), daemon=True).start()

        handler = GtkFileDrop(deliver, gtk.drag_finish)
        view.connect("drag-drop", handler.drop)
        view.connect("drag-data-received", handler.received)
        return
    from webview.dom import DOMEventHandler  # type: ignore[import-not-found, unused-ignore]

    window.dom.document.events.drop += DOMEventHandler(
        lambda event: forward_drop(window, event), prevent_default=True
    )
