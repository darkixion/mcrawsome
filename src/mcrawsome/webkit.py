"""Cancel GTK JavaScript calls when their native window is destroyed.

pywebview 6.2.1 waits on a semaphore that its GTK close handler does not release.
A reply arriving during close can therefore keep a bridge thread alive indefinitely.
Keep the replacement on the individual GTK view, with the same result conversion.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import threading
from typing import Any

_LOG = logging.getLogger(__name__)


class JavaScriptCalls:
    def __init__(self, view: Any, glib: Any, cancellable: Any) -> None:
        self.view = view
        self.glib = glib
        self.cancellable = cancellable
        self._lock = threading.Lock()
        self._pending: set[threading.Event] = set()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.cancellable.cancel()
            for done in self._pending:
                done.set()
            self._pending.clear()

    def request_fullscreen(self, element_id: object, timeout: float = 3.0) -> bool:
        if not isinstance(element_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", element_id):
            return False
        body = f"""
            const viewport = document.getElementById({json.dumps(element_id)});
            if (!viewport || viewport.dataset.mcrawsomeViewport !== 'true' ||
                typeof viewport.requestFullscreen !== 'function') return false;
            try {{
                await viewport.requestFullscreen();
                return document.fullscreenElement === viewport;
            }} catch {{ return false; }}
        """
        return self._evaluate(body, False, timeout=timeout, as_function=True) is True

    def evaluate(self, script: str, parse_json: bool = True) -> Any:
        return self._evaluate(script, parse_json)

    def _evaluate(
        self, script: str, parse_json: bool, *, timeout: float | None = None, as_function: bool = False
    ) -> Any:
        done = threading.Event()
        result = None
        with self._lock:
            if self._closed:
                return None
            self._pending.add(done)

        def complete(webview: Any, task: Any) -> None:
            nonlocal result
            try:
                finish = (
                    webview.call_async_javascript_function_finish
                    if as_function
                    else webview.evaluate_javascript_finish
                )
                value = finish(task)
                if not done.is_set():
                    converted = self.view._convert_js_value(value)
                    result = json.loads(converted) if parse_json and converted else converted
            except Exception:
                if not done.is_set():
                    _LOG.exception("Could not read the JavaScript result")
            finally:
                done.set()

        def run() -> bool:
            if not done.is_set():
                try:
                    method = (
                        self.view.webview.call_async_javascript_function
                        if as_function
                        else self.view.webview.evaluate_javascript
                    )
                    method(
                        **({"body": script, "arguments": None} if as_function else {"script": script}),
                        length=len(script),
                        world_name=None,
                        source_uri=None,
                        cancellable=self.cancellable,
                        callback=complete,
                    )
                except Exception:
                    _LOG.exception("Could not evaluate JavaScript")
                    done.set()
            return False

        try:
            self.glib.idle_add(run)
            if not done.wait(timeout):
                done.set()
        finally:
            with self._lock:
                self._pending.discard(done)
        return result


def attach_shutdown(window: Any) -> None:
    """Called by pywebview's synchronous before_show event, on GTK's main thread."""
    if window.gui.renderer != "gtkwebkit2":
        return
    glib = importlib.import_module("gi.repository.GLib")
    gio = importlib.import_module("gi.repository.Gio")
    view = window.gui.BrowserView.instances[window.uid]
    calls = JavaScriptCalls(view, glib, gio.Cancellable())
    view.evaluate_js = calls.evaluate
    view.request_fullscreen = calls.request_fullscreen
    window.native.connect("destroy", lambda native: calls.close())


def request_fullscreen(window: Any, element_id: object) -> bool:
    """Run the viewport request in WebKit's native user-gesture context, awaiting its result."""
    if window is None or window.gui.renderer != "gtkwebkit2":
        return False
    view = window.gui.BrowserView.instances.get(window.uid)
    return bool(view is not None and view.request_fullscreen(element_id))
