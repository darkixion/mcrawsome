"""The GTK 4 window as the existing code asks for it: pywebview's window methods on a Gtk.ApplicationWindow
and its WebKitGTK 6.0 view.

`Bridge`, `GuiInstance`, `gui_drop._forward_paths` and `offer_restart` call these from their own threads. Each
request runs on GTK's main loop while its caller waits; on GTK's main thread that wait could never end, so a
request there fails at once. `close()` cancels what is pending and releases its callers.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Sequence
from typing import Any

_LOG = logging.getLogger(__name__)

OPEN, FOLDER, SAVE = 10, 20, 30  # pywebview's FileDialog values, which the callers pass
# The longest a caller waits for the page; dialogs wait for the user.
EVALUATE_S = 10.0
FULLSCREEN_S = 3.0
# pywebview's "Description (*.ext;*.other)" (webview.util.parse_file_type)
_FILE_TYPE = re.compile(r"^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*(?:\.(?:\w+|\*))*)*)\)$")


def file_filter(file_type: str) -> tuple[str, list[str]]:
    """The name and glob patterns of a pywebview file type, with pywebview's ValueError for others."""
    match = _FILE_TYPE.search(file_type)
    if match is None:
        raise ValueError(f"{file_type} is not a valid file filter")
    return match.group(1).rstrip(), match.group(2).split(";")


def _file_method(dialog_type: int, allow_multiple: bool) -> tuple[str, str, bool]:
    """pywebview's title for the dialog, the Gtk.FileDialog method, and whether that answers a list."""
    if dialog_type == OPEN:
        return ("Open files", "open_multiple", True) if allow_multiple else ("Open file", "open", False)
    if dialog_type == FOLDER:
        method = "select_multiple_folders" if allow_multiple else "select_folder"
        return "Open folder", method, allow_multiple
    if dialog_type == SAVE:
        return "Save file", "save", False  # one file, as pywebview saves whatever allow_multiple says
    raise ValueError(f"{dialog_type} is not a file dialog type")


def _paths(chosen: Any, many: bool) -> tuple[str, ...] | None:
    """A Gio.File, or a Gio.ListModel of them, as pywebview's tuple of local paths; None without one."""
    if chosen is None:
        return None
    files = [chosen.get_item(index) for index in range(chosen.get_n_items())] if many else [chosen]
    paths = tuple(path for file in files if (path := file.get_path()) is not None)
    return paths or None


def _decoded(value: Any) -> Any:
    """A JavaScriptCore.Value decoded from JSON; None for undefined, null and what JSON cannot hold."""
    if value is None or value.is_undefined() or value.is_null():
        return None
    text = value.to_json(0)
    return None if text is None else json.loads(text)


def _confirmed(value: Any) -> bool:
    """Only JavaScript's true, as webkit.py's `is True`."""
    return value is not None and bool(value.is_boolean() and value.to_boolean())


def _fullscreen_body(element_id: object) -> str | None:
    """webkit.py's request for the viewport element, with its validation of the id; None when invalid."""
    if not isinstance(element_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", element_id):
        return None
    return f"""
        const viewport = document.getElementById({json.dumps(element_id)});
        if (!viewport || viewport.dataset.mcrawsomeViewport !== 'true' ||
            typeof viewport.requestFullscreen !== 'function') return false;
        try {{
            await viewport.requestFullscreen();
            return document.fullscreenElement === viewport;
        }} catch {{ return false; }}
    """


class Gtk4Host:
    """pywebview's window methods for the callers in gui.py, gui_instance.py and gui_drop.py."""

    def __init__(self, window: Any, view: Any) -> None:
        self.window = window
        self.view = view
        self._glib = importlib.import_module("gi.repository.GLib")
        self._gio = importlib.import_module("gi.repository.Gio")
        self._gtk = importlib.import_module("gi.repository.Gtk")
        self._cancellable = self._gio.Cancellable()
        self._lock = threading.Lock()
        self._pending: set[threading.Event] = set()
        self._closed = False

    def create_file_dialog(
        self,
        dialog_type: int = OPEN,
        directory: str = "",
        allow_multiple: bool = False,
        save_filename: str = "",
        file_types: Sequence[str] = (),
    ) -> tuple[str, ...] | None:
        """pywebview's file dialog: a tuple of the chosen paths, or None when cancelled."""
        kinds = [file_filter(kind) for kind in file_types]
        title, method, many = _file_method(dialog_type, allow_multiple)
        folder = directory if directory and os.path.isdir(directory) else ""
        name = save_filename if dialog_type == SAVE else ""

        def start(callback: Callable[[Any, Any], None]) -> None:
            dialog = self._file_dialog(title, folder, name, kinds)
            getattr(dialog, method)(self.window, self._cancellable, callback)

        def finish(dialog: Any, result: Any) -> tuple[str, ...] | None:
            return _paths(getattr(dialog, f"{method}_finish")(result), many)

        return self._call(start, finish, None, None)

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        """pywebview's confirmation: True for OK, False for Cancel."""

        def start(callback: Callable[[Any, Any], None]) -> None:
            dialog = self._gtk.AlertDialog(
                message=title, detail=message, buttons=["Cancel", "OK"], cancel_button=0, default_button=1
            )
            dialog.choose(self.window, self._cancellable, callback)

        return self._call(start, lambda dialog, result: dialog.choose_finish(result) == 1, False, None)

    def evaluate_js(self, script: str, *, timeout: float = EVALUATE_S) -> Any:
        """The script's value from JSON; None for undefined or null, and on error, timeout or close."""

        def start(callback: Callable[[Any, Any], None]) -> None:
            self.view.evaluate_javascript(script, -1, None, None, self._cancellable, callback)

        def finish(view: Any, result: Any) -> Any:
            try:
                return _decoded(view.evaluate_javascript_finish(result))
            except self._glib.Error as error:
                # The script ran, but its value (a promise, say) cannot be returned: None, as pywebview gives.
                webkit = importlib.import_module("gi.repository.WebKit")
                if error.matches(webkit.JavascriptError.quark(), webkit.JavascriptError.INVALID_RESULT):
                    return None
                raise

        return self._call(start, finish, None, timeout)

    def request_fullscreen(self, element_id: object, *, timeout: float = FULLSCREEN_S) -> bool:
        """Run the viewport request in WebKit's native user-gesture context, awaiting its result."""
        body = _fullscreen_body(element_id)
        if body is None:
            return False

        def start(callback: Callable[[Any, Any], None]) -> None:
            self.view.call_async_javascript_function(body, -1, None, None, None, self._cancellable, callback)

        def finish(view: Any, result: Any) -> bool:
            return _confirmed(view.call_async_javascript_function_finish(result))

        return self._call(start, finish, False, timeout)

    def restore(self) -> None:
        """Present the window. As in pywebview, this, show() and load_url() return once scheduled."""
        self._schedule(self.window.present)

    def show(self) -> None:
        self._schedule(self.window.present)

    def load_url(self, url: str) -> None:
        self._schedule(lambda: self.view.load_uri(url))

    def close(self) -> None:
        """Release every waiting caller and cancel GTK's pending work; later calls fail at once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending, self._pending = self._pending, set()
        for done in pending:
            done.set()
        # Cancelling runs GTK's handlers in the cancelling thread.
        if self._glib.MainContext.default().is_owner():
            self._cancellable.cancel()
        else:
            self._glib.idle_add(self._cancellable.cancel)

    def _file_dialog(self, title: str, folder: str, name: str, kinds: list[tuple[str, list[str]]]) -> Any:
        """A Gtk.FileDialog set up as pywebview's GTK 3 chooser was."""
        gtk = self._gtk
        dialog = gtk.FileDialog()
        dialog.set_title(title)
        if folder:
            dialog.set_initial_folder(self._gio.File.new_for_path(folder))
        if name:
            dialog.set_initial_name(name)
        if kinds:
            filters = self._gio.ListStore.new(gtk.FileFilter)
            for label, patterns in kinds:
                kind = gtk.FileFilter()
                kind.set_name(label)
                for pattern in patterns:
                    kind.add_pattern(pattern)
                filters.append(kind)
            dialog.set_filters(filters)
            dialog.set_default_filter(filters.get_item(0))
        return dialog

    def _schedule(self, action: Callable[[], object]) -> None:
        """Run `action` on GTK's main loop unless the host closes first."""

        def run() -> bool:
            if not self._closed:
                action()
            return False

        if not self._closed:
            self._glib.idle_add(run)

    def _call[T](
        self,
        start: Callable[[Callable[[Any, Any], None]], None],
        finish: Callable[[Any, Any], T],
        failure: T,
        timeout: float | None,
    ) -> T:
        """Run `start(callback)` on GTK's main loop and wait for `finish(source, result)` in that callback.

        `failure` answers an error, a dismissed dialog, the timeout and close, and at once on GTK's main
        thread, whose loop could not run the request while this waits.
        """
        done = threading.Event()
        answers: list[T] = []
        with self._lock:
            if self._closed:
                return failure
            if self._glib.MainContext.default().is_owner():
                _LOG.error(
                    "Refused a window request on GTK's main thread: it would wait for itself", stack_info=True
                )
                return failure
            self._pending.add(done)

        def complete(source: Any, result: Any) -> None:
            try:
                answer = finish(source, result)
            except Exception as error:
                if not done.is_set() and not self._dismissed(error):
                    _LOG.exception("The window could not complete a request")
                answer = failure
            if not done.is_set():
                answers.append(answer)
                done.set()

        def run() -> bool:
            if not done.is_set():
                try:
                    start(complete)
                except Exception:
                    _LOG.exception("The window could not start a request")
                    done.set()
            return False

        try:
            self._glib.idle_add(run)
            if not done.wait(timeout):
                done.set()
        finally:
            with self._lock:
                self._pending.discard(done)
        return answers[0] if answers else failure

    def _dismissed(self, error: Exception) -> bool:
        """The user closed a dialog without choosing: pywebview's None or False rather than a failure."""
        reason = self._gtk.DialogError
        return isinstance(error, self._glib.Error) and bool(error.matches(reason.quark(), reason.DISMISSED))
