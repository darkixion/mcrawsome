"""A closed GTK window must release threads waiting for JavaScript results."""

import queue
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome.webkit import JavaScriptCalls, attach_shutdown


class FakeWebKit:
    def __init__(self) -> None:
        self.scheduled: queue.Queue[Any] = queue.Queue()
        self.callbacks: list[Any] = []
        self.requests: list[dict[str, Any]] = []
        self.cancelled = False
        self.fail = False
        self.calls = JavaScriptCalls(
            SimpleNamespace(webview=self, _convert_js_value=lambda value: value),
            SimpleNamespace(idle_add=self.scheduled.put),
            self,
        )

    def cancel(self) -> None:
        self.cancelled = True

    def evaluate_javascript(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("closed WebKit view")
        self.requests.append(kwargs)
        self.callbacks.append(kwargs["callback"])

    def evaluate_javascript_finish(self, value: str) -> str:
        return value


@pytest.mark.parametrize(("parse_json", "expected"), [(True, {"answer": 42}), (False, '{"answer":42}')])
def test_javascript_result_reaches_the_waiting_thread(parse_json: bool, expected: Any) -> None:
    fake = FakeWebKit()
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(fake.calls.evaluate, "42", parse_json)
        fake.scheduled.get(timeout=2)()
        fake.callbacks.pop()(fake, '{"answer":42}')
        assert task.result(2) == expected


def test_destroy_cancels_every_pending_evaluation_and_rejects_late_calls() -> None:
    fake = FakeWebKit()
    with ThreadPoolExecutor(2) as pool:
        tasks = [pool.submit(fake.calls.evaluate, "42", True) for _ in range(2)]
        # Run GTK work while the worker threads wait, then destroy the native window.
        for _ in range(2):
            fake.scheduled.get(timeout=2)()
        assert len(fake.callbacks) == 2
        fake.calls.close()
        assert [task.result(2) for task in tasks] == [None, None]
    assert fake.cancelled
    assert fake.calls.evaluate("late", True) is None
    assert fake.scheduled.empty()
    # A completion already dispatched by WebKit is harmless after destruction.
    fake.callbacks.pop()(fake, "42")


def test_destroy_before_gtk_dispatch_does_not_touch_the_dead_view() -> None:
    fake = FakeWebKit()
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(fake.calls.evaluate, "42", True)
        scheduled = fake.scheduled.get(timeout=2)
        fake.calls.close()
        assert task.result(2) is None
    scheduled()
    assert not fake.callbacks


def test_a_webkit_dispatch_failure_also_releases_the_caller() -> None:
    fake = FakeWebKit()
    fake.fail = True
    fake.calls.glib.idle_add = lambda action: action()
    assert fake.calls.evaluate("42", True) is None


def test_shutdown_handler_is_scoped_to_the_native_window(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, other = FakeWebKit(), FakeWebKit()
    handlers: dict[str, Any] = {}
    view = SimpleNamespace(webview=fake, _convert_js_value=lambda value: value)
    window = SimpleNamespace(
        uid="test",
        gui=SimpleNamespace(
            renderer="gtkwebkit2", BrowserView=SimpleNamespace(instances={"test": view, "other": other})
        ),
        native=SimpleNamespace(connect=lambda signal, handler: handlers.update({signal: handler})),
    )
    monkeypatch.setattr(
        "mcrawsome.webkit.importlib.import_module",
        lambda name: {
            "gi.repository.GLib": fake.calls.glib,
            "gi.repository.Gio": SimpleNamespace(Cancellable=lambda: fake),
        }[name],
    )
    attach_shutdown(window)
    handlers["destroy"](window.native)
    assert view.evaluate_js("42", True) is None
    assert fake.cancelled
    assert not other.cancelled


@pytest.mark.parametrize("answer", [True, False, None, "true", {"error": "permission"}])
def test_native_fullscreen_waits_for_browser_confirmation(answer: Any) -> None:
    fake = FakeWebKit()
    fake.call_async_javascript_function = fake.evaluate_javascript  # type: ignore[attr-defined]
    fake.call_async_javascript_function_finish = fake.evaluate_javascript_finish  # type: ignore[attr-defined]
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(fake.calls.request_fullscreen, "mcrawsome-viewport-c0")
        fake.scheduled.get(timeout=2)()
        body = fake.requests[-1]["body"]
        assert 'document.getElementById("mcrawsome-viewport-c0")' in body
        assert "mcrawsomeViewport" in body
        assert "await viewport.requestFullscreen()" in body
        fake.callbacks.pop()(fake, answer)
        assert task.result(2) is (answer is True)


@pytest.mark.parametrize("invalid", ["", 'x");alert(1);//', "a" * 129, None])
def test_native_fullscreen_refuses_invalid_viewport_ids(invalid: object) -> None:
    fake = FakeWebKit()
    assert not fake.calls.request_fullscreen(invalid)
    assert fake.scheduled.empty()


def test_native_fullscreen_timeout_and_close_release_the_waiter() -> None:
    fake = FakeWebKit()
    fake.call_async_javascript_function = fake.evaluate_javascript  # type: ignore[attr-defined]
    fake.call_async_javascript_function_finish = fake.evaluate_javascript_finish  # type: ignore[attr-defined]
    assert not fake.calls.request_fullscreen("mcrawsome-viewport-c0", timeout=0)
    fake.scheduled.get(timeout=1)()  # timed-out work cannot start later
    assert not fake.callbacks
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(fake.calls.request_fullscreen, "mcrawsome-viewport-c0")
        fake.scheduled.get(timeout=1)()
        fake.calls.close()
        assert not task.result(1)
    assert fake.cancelled
    assert not fake.calls.request_fullscreen("mcrawsome-viewport-c0")
    fake.callbacks.pop()(fake, True)  # a late completion cannot revive a closed request
