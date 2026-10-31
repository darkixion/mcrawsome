"""The GTK 4 window answers the existing callers as pywebview's window does: each request runs on GTK's main
loop while its caller waits, and close releases every caller."""

from __future__ import annotations

import enum
import json
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome.gtk4.host import Gtk4Host, file_filter
from mcrawsome.webkit import JavaScriptCalls

PENDING = object()  # an operation GTK answers only when the test says, or never
DIALOG_ERROR, WEBKIT_ERROR = 7, 8  # fake error domain quarks
FAILED, CANCELLED, DISMISSED = 0, 1, 2  # Gtk.DialogError


class FileDialog(enum.IntEnum):
    """pywebview's dialog types, as the callers pass them."""

    OPEN = 10
    FOLDER = 20
    SAVE = 30


class FakeGError(Exception):
    """GLib.Error: a domain quark and a code."""

    def __init__(self, domain: int, code: int) -> None:
        super().__init__(f"error {code} in domain {domain}")
        self.domain = domain
        self.code = code

    def matches(self, domain: int, code: int) -> bool:
        return (self.domain, self.code) == (domain, code)


class FakeResult:
    """Gio.AsyncResult: what the matching finish call returns or raises."""

    def __init__(self, method: str, outcome: Any) -> None:
        self.method = method
        self.outcome = outcome

    def answer(self, method: str) -> Any:
        assert method == self.method, f"{method}_finish for a {self.method} result"
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@dataclass
class Operation:
    """An asynchronous GTK or WebKit call, as started."""

    source: Any
    method: str
    arguments: tuple[Any, ...]
    cancellable: Any
    callback: Callable[[Any, Any], None]
    answered: bool = False


class FakeFile:
    """Gio.File; a location without a local path has None."""

    def __init__(self, path: str | None) -> None:
        self.path = path

    def get_path(self) -> str | None:
        return self.path


class FakeStore:
    """Gio.ListStore, and the Gio.ListModel of files that several choices answer."""

    def __init__(self, *items: Any, item_type: Any = None) -> None:
        self.items = list(items)
        self.item_type = item_type

    def append(self, item: Any) -> None:
        self.items.append(item)

    def get_n_items(self) -> int:
        return len(self.items)

    def get_item(self, position: int) -> Any:
        return self.items[position] if position < len(self.items) else None


class FakeFileFilter:
    """Gtk.FileFilter: a name and glob patterns."""

    def __init__(self) -> None:
        self.name = ""
        self.patterns: list[str] = []

    def set_name(self, name: str) -> None:
        self.name = name

    def add_pattern(self, pattern: str) -> None:
        self.patterns.append(pattern)


class FakeValue:
    """JavaScriptCore.Value: JSON.stringify's text for it (None when there is none), or undefined."""

    def __init__(self, text: str | None, *, undefined: bool = False) -> None:
        self.text = text
        self.undefined = undefined

    def is_undefined(self) -> bool:
        return self.undefined

    def is_null(self) -> bool:
        return self.text == "null"

    def is_boolean(self) -> bool:
        return self.text in ("true", "false")

    def to_boolean(self) -> bool:  # JavaScript's truthiness, for the values these tests use
        return not self.undefined and (self.text is None or bool(json.loads(self.text)))

    def to_json(self, indent: int) -> str | None:
        assert indent == 0
        return self.text


class FakeGtk4:
    """GTK's main loop on its own thread, with fake GLib, Gio and Gtk modules, window and WebKit view. Each
    asynchronous call answers `answer` on a later iteration: a value, an exception to raise from its finish
    call, or PENDING to wait for `complete` or cancellation."""

    host: Gtk4Host

    def __init__(self) -> None:
        self.work: queue.Queue[Callable[[], object] | None] = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="fake-gtk-main-loop", daemon=True)
        self.scheduled: list[Callable[..., object]] = []
        self.errors: list[Exception] = []
        self.answer: Any = PENDING
        self.operations: list[Operation] = []
        self.dialogs: list[Any] = []
        self.presented: list[threading.Thread] = []
        self.cancellable = FakeCancellable(self)
        self.window = SimpleNamespace(present=lambda: self.presented.append(threading.current_thread()))
        self.view = FakeView(self)
        self.glib = SimpleNamespace(
            idle_add=self.idle_add, MainContext=SimpleNamespace(default=lambda: self), Error=FakeGError
        )
        self.gio = SimpleNamespace(
            Cancellable=lambda: self.cancellable,
            File=SimpleNamespace(new_for_path=FakeFile),
            ListStore=SimpleNamespace(new=lambda item_type: FakeStore(item_type=item_type)),
        )
        self.gtk = SimpleNamespace(
            FileDialog=lambda: FakeFileDialog(self),
            FileFilter=FakeFileFilter,
            AlertDialog=lambda **properties: FakeAlertDialog(self, properties),
            DialogError=SimpleNamespace(
                quark=lambda: DIALOG_ERROR, FAILED=FAILED, CANCELLED=CANCELLED, DISMISSED=DISMISSED
            ),
        )
        self.thread.start()

    def _loop(self) -> None:
        while (work := self.work.get()) is not None:
            try:
                work()
            except Exception as error:  # PyGObject reports these and carries on
                self.errors.append(error)

    def idle_add(self, work: Callable[..., object], *args: Any) -> int:
        self.scheduled.append(work)
        self.work.put(lambda: work(*args))
        return len(self.scheduled)

    def is_owner(self) -> bool:  # GLib.MainContext.default().is_owner()
        return threading.current_thread() is self.thread

    def begin(self, source: Any, method: str, *arguments: Any) -> None:
        """Start `source.method(*arguments)`, whose last two are a cancellable and a callback."""
        assert threading.current_thread() is self.thread, "GTK started outside its main loop"
        *started, cancellable, callback = arguments
        self.operations.append(Operation(source, method, tuple(started), cancellable, callback))
        if self.answer is not PENDING:
            self.complete(self.operations[-1], self.answer)

    def complete(self, operation: Operation, outcome: Any) -> None:
        """Answer on a later iteration of the loop, as GTK and WebKit do."""
        operation.answered = True
        self.idle_add(operation.callback, operation.source, FakeResult(operation.method, outcome))

    def submit[T](self, function: Callable[..., T], *args: Any) -> Future[T]:
        """A call from a thread other than GTK's: a daemon, so a call left waiting cannot hold the tests."""
        answer: Future[T] = Future()

        def run() -> None:
            try:
                answer.set_result(function(*args))
            except Exception as error:
                answer.set_exception(error)

        threading.Thread(target=run, daemon=True).start()
        return answer

    def call[T](self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """A call from a thread other than GTK's, failing instead of hanging."""
        return self.submit(lambda: function(*args, **kwargs)).result(5)

    def on_loop[T](self, function: Callable[[], T]) -> T:
        """A call on GTK's main thread."""
        answer: Future[T] = Future()
        self.idle_add(lambda: answer.set_result(function()))
        return answer.result(5)

    def drain(self) -> None:
        """Wait for the work already scheduled, and what it schedules in turn."""
        for _ in range(3):
            self.on_loop(lambda: None)

    def wait_for(self, *, operations: int = 0, scheduled: int = 0) -> None:
        deadline = time.monotonic() + 5
        while len(self.operations) < operations or len(self.scheduled) < scheduled:
            assert time.monotonic() < deadline, "GTK did not start the expected work"
            time.sleep(0.005)

    def stop(self) -> None:
        self.work.put(None)
        self.thread.join(5)


class FakeCancellable:
    """Gio.Cancellable: cancelling ends every unanswered operation with GTK's cancellation error."""

    def __init__(self, fake: FakeGtk4) -> None:
        self.fake = fake
        self.cancelled_on: threading.Thread | None = None

    def cancel(self) -> None:
        self.cancelled_on = threading.current_thread()
        for operation in self.fake.operations:
            if not operation.answered:
                self.fake.complete(operation, FakeGError(DIALOG_ERROR, CANCELLED))


class FakeView:
    """WebKit.WebView from WebKitGTK 6.0, called with positional arguments as the host calls it."""

    def __init__(self, fake: FakeGtk4) -> None:
        self.fake = fake
        self.loaded: list[tuple[str, threading.Thread]] = []
        self.fail = False

    def evaluate_javascript(self, *arguments: Any) -> None:
        if self.fail:
            raise RuntimeError("closed WebKit view")
        self.fake.begin(self, "evaluate_javascript", *arguments)

    def evaluate_javascript_finish(self, result: FakeResult) -> Any:
        return result.answer("evaluate_javascript")

    def call_async_javascript_function(self, *arguments: Any) -> None:
        self.fake.begin(self, "call_async_javascript_function", *arguments)

    def call_async_javascript_function_finish(self, result: FakeResult) -> Any:
        return result.answer("call_async_javascript_function")

    def load_uri(self, url: str) -> None:
        self.loaded.append((url, threading.current_thread()))


class FakeFileDialog:
    """Gtk.FileDialog: records its setup, and each choosing method answers through its own `*_finish`."""

    METHODS = ("open", "open_multiple", "select_folder", "select_multiple_folders", "save")

    def __init__(self, fake: FakeGtk4) -> None:
        self.fake = fake
        self.title = ""
        self.folder: str | None = None
        self.name: str | None = None
        self.filters: FakeStore | None = None
        self.default_filter: FakeFileFilter | None = None
        fake.dialogs.append(self)

    def set_title(self, title: str) -> None:
        self.title = title

    def set_initial_folder(self, folder: FakeFile) -> None:
        self.folder = folder.get_path()

    def set_initial_name(self, name: str) -> None:
        self.name = name

    def set_filters(self, filters: FakeStore) -> None:
        self.filters = filters

    def set_default_filter(self, kind: FakeFileFilter) -> None:
        self.default_filter = kind

    def __getattr__(self, name: str) -> Any:
        method = name.removesuffix("_finish")
        if method not in self.METHODS:
            raise AttributeError(name)
        if name != method:
            return lambda result: result.answer(method)
        return lambda *arguments: self.fake.begin(self, method, *arguments)


class FakeAlertDialog:
    """Gtk.AlertDialog, made from its properties."""

    def __init__(self, fake: FakeGtk4, properties: dict[str, Any]) -> None:
        self.fake = fake
        self.properties = properties
        fake.dialogs.append(self)

    def choose(self, *arguments: Any) -> None:
        self.fake.begin(self, "choose", *arguments)

    def choose_finish(self, result: FakeResult) -> Any:
        return result.answer("choose")


@pytest.fixture
def gtk4(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGtk4]:
    fake = FakeGtk4()
    monkeypatch.setitem(sys.modules, "gi.repository.GLib", fake.glib)
    monkeypatch.setitem(sys.modules, "gi.repository.Gio", fake.gio)
    monkeypatch.setitem(sys.modules, "gi.repository.Gtk", fake.gtk)
    webkit = SimpleNamespace(JavascriptError=SimpleNamespace(quark=lambda: WEBKIT_ERROR, INVALID_RESULT=601))
    monkeypatch.setitem(sys.modules, "gi.repository.WebKit", webkit)
    fake.host = Gtk4Host(fake.window, fake.view)
    yield fake
    fake.host.close()  # releases any caller a failing test left waiting
    fake.stop()
    assert not fake.errors


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def webkit_body(element_id: str) -> str:
    """The body webkit.py sends for the GTK 3 window."""
    bodies: list[str] = []
    webview = SimpleNamespace(call_async_javascript_function=lambda **request: bodies.append(request["body"]))
    calls = JavaScriptCalls(
        SimpleNamespace(webview=webview), SimpleNamespace(idle_add=lambda run: run()), SimpleNamespace()
    )
    calls.request_fullscreen(element_id, timeout=0)
    return bodies[0]


# ---- File dialogs ----


@pytest.mark.parametrize(
    ("dialog_type", "allow_multiple", "method", "title", "answer", "expected"),
    [
        (FileDialog.OPEN, False, "open", "Open file", FakeFile("/clips/a.mcraw"), ("/clips/a.mcraw",)),
        (
            FileDialog.OPEN,
            True,
            "open_multiple",
            "Open files",
            FakeStore(FakeFile("/clips/a.mcraw"), FakeFile("/clips/b.mov")),
            ("/clips/a.mcraw", "/clips/b.mov"),
        ),
        (FileDialog.FOLDER, False, "select_folder", "Open folder", FakeFile("/clips"), ("/clips",)),
        (
            FileDialog.FOLDER,
            True,
            "select_multiple_folders",
            "Open folder",
            FakeStore(FakeFile("/a"), FakeFile("/b")),
            ("/a", "/b"),
        ),
        (FileDialog.SAVE, False, "save", "Save file", FakeFile("/out/report.json"), ("/out/report.json",)),
        (FileDialog.SAVE, True, "save", "Save file", FakeFile("/out/report.json"), ("/out/report.json",)),
    ],
)
def test_each_dialog_type_answers_a_tuple_of_paths_as_pywebview(
    gtk4: FakeGtk4,
    dialog_type: FileDialog,
    allow_multiple: bool,
    method: str,
    title: str,
    answer: Any,
    expected: tuple[str, ...],
) -> None:
    gtk4.answer = answer
    assert gtk4.call(gtk4.host.create_file_dialog, dialog_type, allow_multiple=allow_multiple) == expected
    [operation] = gtk4.operations
    assert (operation.method, operation.arguments, operation.cancellable) == (
        method,
        (gtk4.window,),
        gtk4.cancellable,
    )
    assert gtk4.dialogs[0].title == title


def test_dialogs_open_in_the_folder_with_the_name_and_filters_asked_for(
    gtk4: FakeGtk4, tmp_path: Path
) -> None:
    gtk4.answer = FakeFile(str(tmp_path / "clip.capture.csv"))
    file_types = ("Frame metadata (*.csv)", "MotionCam clips (*.mcraw;*.mp4;*.mov;*.mkv)")
    chosen = gtk4.call(
        gtk4.host.create_file_dialog,
        FileDialog.SAVE,
        directory=str(tmp_path),
        save_filename="clip.capture.csv",
        file_types=file_types,
    )
    assert chosen == (str(tmp_path / "clip.capture.csv"),)
    dialog = gtk4.dialogs[0]
    assert (dialog.folder, dialog.name) == (str(tmp_path), "clip.capture.csv")
    assert dialog.filters.item_type is FakeFileFilter
    assert [(kind.name, kind.patterns) for kind in dialog.filters.items] == [
        ("Frame metadata", ["*.csv"]),
        ("MotionCam clips", ["*.mcraw", "*.mp4", "*.mov", "*.mkv"]),
    ]
    assert dialog.default_filter is dialog.filters.items[0]
    # As pywebview: a missing folder is ignored, only a save dialog is named, and filters only when asked for.
    gtk4.call(
        gtk4.host.create_file_dialog,
        FileDialog.OPEN,
        directory=str(tmp_path / "gone"),
        save_filename="x.json",
    )
    dialog = gtk4.dialogs[1]
    assert (dialog.folder, dialog.name, dialog.filters, dialog.default_filter) == (None, None, None, None)


@pytest.mark.parametrize(
    ("file_type", "expected"),
    [
        (
            "MotionCam clips (*.mcraw;*.mp4;*.mov;*.mkv)",
            ("MotionCam clips", ["*.mcraw", "*.mp4", "*.mov", "*.mkv"]),
        ),
        ("mcrawsome sessions (*.json)", ("mcrawsome sessions", ["*.json"])),
        ("Avid Log Exchange (*.ale)", ("Avid Log Exchange", ["*.ale"])),
        ("All files (*.*)", ("All files", ["*.*"])),
        ("Everything (*)", ("Everything", ["*"])),
        ("Close(*.csv)", ("Close", ["*.csv"])),
    ],
)
def test_file_types_parse_as_pywebview_parses_them(file_type: str, expected: tuple[str, list[str]]) -> None:
    assert file_filter(file_type) == expected


@pytest.mark.parametrize(
    "file_type",
    ["Clips", "Clips (*.mcraw", "Clips (*.mcraw) ", "Clips (mcraw)", "Clips (*.a;rm)", "Clips: (*.a)"],
)
def test_an_invalid_file_type_fails_before_any_dialog_opens(gtk4: FakeGtk4, file_type: str) -> None:
    with pytest.raises(ValueError, match="not a valid file filter"):
        gtk4.call(gtk4.host.create_file_dialog, FileDialog.OPEN, file_types=("JSON (*.json)", file_type))
    with pytest.raises(ValueError, match="not a file dialog type"):
        gtk4.call(gtk4.host.create_file_dialog, 40)
    gtk4.drain()
    assert not gtk4.dialogs


@pytest.mark.parametrize(
    ("answer", "logged"),
    [
        (FakeGError(DIALOG_ERROR, DISMISSED), False),  # Cancel, Escape or the window's close button
        (FakeGError(DIALOG_ERROR, FAILED), True),
        (FakeFile(None), False),  # a remote location without a local path
        (None, False),
    ],
)
def test_a_dialog_closed_without_a_local_choice_answers_none(
    gtk4: FakeGtk4, caplog: pytest.LogCaptureFixture, answer: Any, logged: bool
) -> None:
    gtk4.answer = answer
    assert gtk4.call(gtk4.host.create_file_dialog, FileDialog.OPEN) is None
    assert ("The window could not complete a request" in messages(caplog)) is logged


def test_the_bridge_reads_the_answers_as_it_reads_pywebview(
    gtk4: FakeGtk4, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcrawsome.gui import Bridge

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=FileDialog))
    bridge = Bridge(9000, [])
    bridge._window = gtk4.host
    gtk4.answer = FakeFile("/clips")
    assert gtk4.call(bridge.choose_folder) == "/clips"
    gtk4.answer = FakeStore(FakeFile("/clips/a.mcraw"), FakeFile("/clips/b.mov"))
    assert gtk4.call(bridge.choose_files) == ["/clips/a.mcraw", "/clips/b.mov"]
    target = tmp_path / "positions.gpx"
    gtk4.answer = FakeFile(str(target))
    assert gtk4.call(bridge.save_text, "<gpx/>", "positions.gpx")
    assert target.read_text() == "<gpx/>"
    gtk4.answer = FakeGError(DIALOG_ERROR, DISMISSED)
    assert gtk4.call(bridge.choose_session, True) is None
    assert gtk4.call(bridge.choose_files) == []


# ---- Confirmation ----


@pytest.mark.parametrize(
    ("answer", "expected"), [(1, True), (0, False), (FakeGError(DIALOG_ERROR, DISMISSED), False)]
)
def test_confirmation_is_true_only_for_ok(gtk4: FakeGtk4, answer: Any, expected: bool) -> None:
    gtk4.answer = answer
    assert gtk4.call(gtk4.host.create_confirmation_dialog, "MCRAWsome", "Restart it now?") is expected
    [dialog] = gtk4.dialogs
    assert dialog.properties == {
        "message": "MCRAWsome",
        "detail": "Restart it now?",
        "buttons": ["Cancel", "OK"],
        "cancel_button": 0,
        "default_button": 1,
    }
    [operation] = gtk4.operations
    assert (operation.method, operation.arguments, operation.cancellable) == (
        "choose",
        (gtk4.window,),
        gtk4.cancellable,
    )


# ---- JavaScript ----


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (FakeValue(None, undefined=True), None),
        (FakeValue("null"), None),
        (FakeValue("true"), True),
        (FakeValue("42"), 42),
        (FakeValue('"text"'), "text"),
        (FakeValue('{"paths":["/a"],"skipped":null}'), {"paths": ["/a"], "skipped": None}),
        (FakeValue(None), None),  # a function, which JSON cannot hold
    ],
)
def test_evaluate_js_decodes_the_value_from_json(
    gtk4: FakeGtk4, caplog: pytest.LogCaptureFixture, value: FakeValue, expected: Any
) -> None:
    gtk4.answer = value
    script = "window.dispatchEvent(new CustomEvent('mcrawsome-open-files', {detail: []}));"
    assert gtk4.call(gtk4.host.evaluate_js, script) == expected
    [operation] = gtk4.operations
    assert (operation.method, operation.arguments, operation.cancellable) == (
        "evaluate_javascript",
        (script, -1, None, None),
        gtk4.cancellable,
    )
    assert not caplog.records


def test_evaluate_js_answers_none_for_a_javascript_error_or_timeout(
    gtk4: FakeGtk4, caplog: pytest.LogCaptureFixture
) -> None:
    gtk4.answer = FakeGError(WEBKIT_ERROR, 699)  # an exception in the script
    assert gtk4.call(gtk4.host.evaluate_js, "missing()") is None
    assert messages(caplog) == ["The window could not complete a request"]
    caplog.clear()
    gtk4.answer = PENDING
    assert gtk4.call(gtk4.host.evaluate_js, "1", timeout=0.05) is None
    gtk4.drain()
    for late in gtk4.operations[1:]:  # GTK may start it after the timeout, or never
        gtk4.complete(late, FakeValue("1"))
    gtk4.drain()
    assert not caplog.records


def test_evaluate_js_answers_none_quietly_for_a_value_it_cannot_return(
    gtk4: FakeGtk4, caplog: pytest.LogCaptureFixture
) -> None:
    gtk4.answer = FakeGError(WEBKIT_ERROR, 601)  # a promise, say: the script ran
    assert gtk4.call(gtk4.host.evaluate_js, "(async () => 1)()") is None
    assert not caplog.records


def test_a_request_gtk_cannot_start_releases_its_caller(
    gtk4: FakeGtk4, caplog: pytest.LogCaptureFixture
) -> None:
    gtk4.view.fail = True
    assert gtk4.call(gtk4.host.evaluate_js, "1") is None
    assert messages(caplog) == ["The window could not start a request"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (FakeValue("true"), True),
        (FakeValue("false"), False),
        (FakeValue('"true"'), False),
        (FakeValue('{"error":"permission"}'), False),
        (FakeValue("null"), False),
        (FakeGError(WEBKIT_ERROR, 699), False),
    ],
)
def test_fullscreen_runs_webkits_body_in_its_gesture_context(
    gtk4: FakeGtk4, answer: Any, expected: bool
) -> None:
    gtk4.answer = answer
    assert gtk4.call(gtk4.host.request_fullscreen, "mcrawsome-viewport-c0") is expected
    [operation] = gtk4.operations
    body, *rest = operation.arguments
    assert (operation.method, rest, operation.cancellable) == (
        "call_async_javascript_function",
        [-1, None, None, None],
        gtk4.cancellable,
    )
    assert body.split() == webkit_body("mcrawsome-viewport-c0").split()


@pytest.mark.parametrize("invalid", ["", 'x");alert(1);//', "a" * 129, None, 7])
def test_fullscreen_refuses_invalid_viewport_ids(gtk4: FakeGtk4, invalid: object) -> None:
    assert gtk4.call(gtk4.host.request_fullscreen, invalid) is False
    gtk4.drain()
    assert not gtk4.operations


def test_fullscreen_waits_a_bounded_time(gtk4: FakeGtk4) -> None:
    assert gtk4.call(gtk4.host.request_fullscreen, "mcrawsome-viewport-c0", timeout=0.05) is False


# ---- The window ----


def test_restore_show_and_load_url_run_on_gtk_main_loop_from_any_thread(gtk4: FakeGtk4) -> None:
    gtk4.call(gtk4.host.restore)
    gtk4.call(gtk4.host.show)
    gtk4.on_loop(gtk4.host.show)
    gtk4.call(gtk4.host.load_url, "http://127.0.0.1:8765/?open=1#/library")
    gtk4.drain()
    assert gtk4.presented == [gtk4.thread] * 3
    assert gtk4.view.loaded == [("http://127.0.0.1:8765/?open=1#/library", gtk4.thread)]


def test_requests_on_gtk_main_thread_answer_at_once_rather_than_wait_for_it(
    gtk4: FakeGtk4, caplog: pytest.LogCaptureFixture
) -> None:
    host = gtk4.host
    answers = gtk4.on_loop(
        lambda: (
            host.create_file_dialog(FileDialog.OPEN),
            host.create_confirmation_dialog("MCRAWsome", "Restart it now?"),
            host.evaluate_js("1"),
            host.request_fullscreen("mcrawsome-viewport-c0"),
        )
    )
    assert answers == (None, False, None, False)
    gtk4.drain()
    assert not gtk4.operations
    assert len(messages(caplog)) == 4
    assert all("main thread" in message for message in messages(caplog))


# ---- Close ----


def test_close_releases_every_waiting_caller_and_cancels_on_gtk_main_loop(
    gtk4: FakeGtk4, caplog: pytest.LogCaptureFixture
) -> None:
    host = gtk4.host
    waiting: list[tuple[Future[Any], Any]] = [
        (gtk4.submit(host.create_file_dialog, FileDialog.OPEN), None),
        (gtk4.submit(host.create_confirmation_dialog, "MCRAWsome", "Restart it now?"), False),
        (gtk4.submit(host.evaluate_js, "1"), None),
        (gtk4.submit(host.request_fullscreen, "mcrawsome-viewport-c0"), False),
    ]
    gtk4.wait_for(operations=4)
    host.close()
    assert [task.result(2) for task, _ in waiting] == [failure for _, failure in waiting]
    gtk4.drain()  # GTK's cancellation errors arrive after close and are dropped quietly
    assert gtk4.cancellable.cancelled_on is gtk4.thread
    assert all(operation.answered for operation in gtk4.operations)
    assert not caplog.records


def test_close_on_gtk_main_thread_cancels_at_once(gtk4: FakeGtk4) -> None:
    def close() -> threading.Thread | None:
        gtk4.host.close()
        return gtk4.cancellable.cancelled_on

    assert gtk4.on_loop(close) is gtk4.thread


def test_after_close_every_call_answers_at_once_and_touches_nothing(gtk4: FakeGtk4) -> None:
    host = gtk4.host
    host.close()
    host.close()
    assert gtk4.call(host.create_file_dialog, FileDialog.FOLDER) is None
    assert gtk4.call(host.create_confirmation_dialog, "MCRAWsome", "Restart it now?") is False
    assert gtk4.call(host.evaluate_js, "1") is None
    assert gtk4.call(host.request_fullscreen, "mcrawsome-viewport-c0") is False
    host.restore()
    host.show()
    host.load_url("http://127.0.0.1:8765/")
    gtk4.drain()
    assert (gtk4.dialogs, gtk4.operations, gtk4.presented, gtk4.view.loaded) == ([], [], [], [])


def test_work_scheduled_before_close_never_reaches_the_window(gtk4: FakeGtk4) -> None:
    busy = threading.Event()
    gtk4.idle_add(busy.wait, 5)
    task = gtk4.submit(gtk4.host.evaluate_js, "1")
    gtk4.host.restore()
    gtk4.host.load_url("http://127.0.0.1:8765/")
    gtk4.wait_for(scheduled=4)
    gtk4.host.close()
    assert task.result(2) is None
    busy.set()
    gtk4.drain()
    assert (gtk4.operations, gtk4.presented, gtk4.view.loaded) == ([], [], [])
