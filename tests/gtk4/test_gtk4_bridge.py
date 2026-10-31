"""The GTK 4 window's page API: script messages run on a thread pool and are answered once each."""

import functools
import json
import logging
import queue
import sys
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome.gtk4.bridge import ScriptBridge, bridge_script, methods

CONTEXT = object()  # the JavaScriptCore context of a posted message
CLOSING = [("error", "The window is closing")]


class Api:
    """A js_api object like gui.Bridge."""

    port = 8642

    def __init__(self) -> None:
        self.waiting = threading.Semaphore(0)
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []

    def add(self, first: int, second: int) -> int:
        self.threads.append(threading.current_thread())
        return first + second

    def nothing(self) -> None:
        return None

    def fail(self, message: str) -> None:
        raise ValueError(message)

    def silent(self) -> None:
        raise RuntimeError

    def unserialisable(self) -> object:
        return object()

    def ratio(self) -> float:
        return float("nan")

    def choose(self) -> str:
        """Wait, as a file dialog waits for the user."""
        self.waiting.release()
        self.release.wait(5)
        return "chosen"

    def _private(self) -> None:
        raise AssertionError("the page cannot call private methods")


class Reply:
    """WebKit's ScriptMessageReply, recording its answers."""

    def __init__(self, *, fail: bool = False) -> None:
        self.answers: list[tuple[str, Any]] = []
        self.fail = fail

    def return_value(self, value: Any) -> None:
        if self.fail:
            raise RuntimeError("the page has gone")
        self.answers.append(("value", value))

    def return_error_message(self, message: str) -> None:
        self.answers.append(("error", message))


class Value:
    """JavaScriptCore's Value for a posted message: a string is its JSON text already, and None stands for
    a value JSON cannot represent."""

    def __init__(self, message: object) -> None:
        self.text = message if message is None or isinstance(message, str) else json.dumps(message)

    def to_json(self, indent: int) -> str | None:
        assert indent == 0
        return self.text

    def get_context(self) -> object:
        return CONTEXT


class Manager:
    """WebKit's UserContentManager."""

    def __init__(self, *, free: bool = True) -> None:
        self.free = free
        self.registered: list[tuple[str, str | None]] = []
        self.handlers: dict[str, Callable[..., bool]] = {}
        self.scripts: list[object] = []

    def register_script_message_handler_with_reply(self, name: str, world_name: str | None) -> bool:
        self.registered.append((name, world_name))
        return self.free

    def connect(self, signal: str, handler: Callable[..., bool]) -> int:
        self.handlers[signal] = handler
        return 1

    def add_script(self, script: object) -> None:
        self.scripts.append(script)


class Window:
    """The bridge with GTK's main loop as a queue: idle callbacks wait until the test runs them."""

    def __init__(self) -> None:
        self.api = Api()
        self.idle: queue.Queue[Callable[[], bool]] = queue.Queue()
        self.bridge = ScriptBridge(self.api, self.idle.put, workers=2)

    def call(self, method: str, *args: Any) -> Reply:
        return self.post({"method": method, "args": list(args)})

    def post(self, message: object, reply: Reply | None = None) -> Reply:
        reply = reply or Reply()
        assert self.bridge.received(None, Value(message), reply) is True
        return reply

    def run_idle(self) -> None:
        """Run the next idle callback, which runs only once."""
        assert self.idle.get(timeout=5)() is False


@pytest.fixture(autouse=True)
def _javascriptcore(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value made from JSON is its context and text."""
    value = SimpleNamespace(new_from_json=lambda context, text: (context, text))
    monkeypatch.setitem(sys.modules, "gi.repository.JavaScriptCore", SimpleNamespace(Value=value))


@pytest.fixture
def window() -> Iterator[Window]:
    window = Window()
    yield window
    window.api.release.set()
    window.bridge.close()


def test_the_api_is_the_public_functions_and_methods_pywebview_exposes() -> None:
    assert methods(Api()) == ["add", "choose", "fail", "nothing", "ratio", "silent", "unserialisable"]
    others = SimpleNamespace(
        zeta=lambda: None, _hidden=lambda: None, builtin=len, kind=Api, partial=functools.partial(len)
    )
    assert methods(others) == ["zeta"]


def test_the_script_defines_a_frozen_api_once_and_announces_it() -> None:
    script = bridge_script(["choose_folder", "preview_present"])
    assert 'for (const method of ["choose_folder", "preview_present"])' in script
    assert "window.webkit.messageHandlers.mcrawsome.postMessage({ method, args })" in script
    assert "if (window.pywebview !== undefined) return;" in script
    assert "window.pywebview = Object.freeze({ api: Object.freeze(api) });" in script
    assert 'if (document.readyState === "loading")' in script
    assert 'document.addEventListener("DOMContentLoaded", ready, { once: true });' in script
    assert 'window.dispatchEvent(new Event("pywebviewready"))' in script


def test_method_names_are_embedded_as_json() -> None:
    names = ['say"hello', "</script>", "line\u2028break"]
    script = bridge_script(names)
    assert f"for (const method of {json.dumps(names)})" in script
    assert script.isascii()


def test_a_call_runs_on_a_worker_and_is_answered_on_the_main_loop(window: Window) -> None:
    reply = window.call("add", 2, 3)
    assert reply.answers == []
    window.run_idle()
    assert reply.answers == [("value", (CONTEXT, "5"))]
    assert window.api.threads[0] is not threading.current_thread()


def test_none_answers_null(window: Window) -> None:
    reply = window.call("nothing")
    window.run_idle()
    assert reply.answers == [("value", (CONTEXT, "null"))]


@pytest.mark.parametrize(
    ("method", "args", "message"),
    [
        ("fail", ["No folder was chosen"], "No folder was chosen"),
        ("silent", [], "RuntimeError"),
        ("add", [1], "missing 1 required positional argument"),
        ("unserialisable", [], "Object of type object is not JSON serializable"),
        ("ratio", [], "Out of range float values are not JSON compliant"),
    ],
)
def test_a_failed_call_is_answered_with_its_error_message(
    window: Window, caplog: pytest.LogCaptureFixture, method: str, args: list[Any], message: str
) -> None:
    reply = window.call(method, *args)
    window.run_idle()
    [(kind, text)] = reply.answers
    assert kind == "error"
    assert message in text
    assert f"The page's call to {method} failed" in caplog.text


def test_an_undecodable_name_is_answered_in_utf_8(window: Window, monkeypatch: pytest.MonkeyPatch) -> None:
    # The log stays out of pytest's capture: pytest-xdist cannot send a lone surrogate between processes.
    logged: list[logging.LogRecord] = []
    handler = logging.Handler()
    monkeypatch.setattr(handler, "emit", logged.append)
    log = logging.getLogger("mcrawsome.gtk4.bridge")
    monkeypatch.setattr(log, "propagate", False)
    monkeypatch.setattr(log, "handlers", [handler])
    reply = window.call("fail", "\udcff.mcraw is not a file")
    window.run_idle()
    assert reply.answers == [("error", "?.mcraw is not a file")]
    assert [record.getMessage() for record in logged] == ["The page's call to fail failed"]


@pytest.mark.parametrize(
    "message",
    [
        None,
        "not JSON",
        "[]",
        '"add"',
        {"method": 1, "args": []},
        {"method": "add"},
        {"method": "add", "args": {"first": 1}},
    ],
)
def test_a_malformed_message_is_refused_at_once(window: Window, message: object) -> None:
    reply = window.post(message)
    assert reply.answers == [("error", "A window call needs a method name and a list of arguments")]
    assert window.idle.empty()


@pytest.mark.parametrize("method", ["missing", "_private", "__init__", "port"])
def test_only_the_api_methods_can_be_called(window: Window, method: str) -> None:
    reply = window.call(method)
    assert reply.answers == [("error", f"The window has no method {method!r}")]
    assert window.idle.empty()


def test_dispatch_is_the_plain_call(window: Window) -> None:
    assert window.bridge.dispatch("add", [2, 3]) == 5
    assert window.api.threads == [threading.current_thread()]
    with pytest.raises(ValueError, match="The window has no method '_private'"):
        window.bridge.dispatch("_private", [])


def test_a_call_waiting_for_the_user_holds_up_neither_the_main_loop_nor_other_calls(window: Window) -> None:
    dialog = window.call("choose")
    assert window.api.waiting.acquire(timeout=5)
    total = window.call("add", 1, 2)
    window.run_idle()
    assert total.answers == [("value", (CONTEXT, "3"))]
    assert dialog.answers == []
    window.api.release.set()
    window.run_idle()
    assert dialog.answers == [("value", (CONTEXT, '"chosen"'))]


def test_close_answers_waiting_calls_once_and_refuses_new_ones(window: Window) -> None:
    running = [window.call("choose"), window.call("choose")]
    for _ in running:
        assert window.api.waiting.acquire(timeout=5)
    queued = window.call("add", 1, 2)  # both workers are busy
    window.bridge.close()
    for _ in range(3):
        window.run_idle()
    assert [reply.answers for reply in [*running, queued]] == [CLOSING] * 3
    assert window.api.threads == []  # the queued call never ran
    assert window.call("add", 1, 2).answers == CLOSING
    assert window.idle.empty()
    window.api.release.set()
    for _ in running:  # their results arrive after close, and are dropped
        window.run_idle()
    assert [reply.answers for reply in running] == [CLOSING] * 2


def test_a_result_ready_before_close_is_the_only_answer(window: Window) -> None:
    reply = window.call("add", 1, 2)
    answer = window.idle.get(timeout=5)
    window.bridge.close()  # the call still waits for GTK's main loop, so close answers it too
    assert answer() is False
    window.run_idle()
    assert reply.answers == [("value", (CONTEXT, "3"))]


def test_a_failed_answer_is_logged_without_raising(window: Window, caplog: pytest.LogCaptureFixture) -> None:
    reply = window.post({"method": "add", "args": [1, 2]}, Reply(fail=True))
    window.run_idle()
    assert reply.answers == []
    assert "Could not answer the page's call" in caplog.text


def test_attach_registers_the_handler_its_signal_and_the_user_script(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[object, ...]] = []

    def user_script(*args: object) -> str:
        created.append(args)
        return "user script"

    monkeypatch.setitem(
        sys.modules,
        "gi.repository.WebKit",
        SimpleNamespace(
            UserScript=SimpleNamespace(new=user_script),
            UserContentInjectedFrames=SimpleNamespace(TOP_FRAME="top frame"),
            UserScriptInjectionTime=SimpleNamespace(START="document start"),
        ),
    )
    manager, taken = Manager(), Manager(free=False)
    bridge = ScriptBridge(Api(), lambda callback: None)
    try:
        bridge.attach(manager)
        with pytest.raises(RuntimeError, match="registered already"):
            bridge.attach(taken)
    finally:
        bridge.close()
    assert manager.registered == [("mcrawsome", None)]
    assert manager.handlers == {"script-message-with-reply-received::mcrawsome": bridge.received}
    assert created == [(bridge_script(bridge.names), "top frame", "document start", None, None)]
    assert manager.scripts == ["user script"]
    assert taken.handlers == {}
    assert taken.scripts == []
