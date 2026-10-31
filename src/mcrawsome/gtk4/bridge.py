"""The page's `window.pywebview.api` on WebKitGTK 6.0, where pywebview cannot run.

A user script defines the API at document start in the top frame. Each call posts `{method, args}` to a
script message handler registered with reply, so `postMessage` returns the call's Promise. The call runs
on a worker thread (file dialogs wait for the user) and is answered once, on GTK's main loop: the Promise
resolves to the JSON result, or rejects with an Error carrying the exception's message.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)
_HANDLER = "mcrawsome"
_CLOSING = "The window is closing"


def methods(api: object) -> list[str]:
    """The public functions and methods of the API object, sorted, as pywebview exposes them."""
    names = []
    for name in dir(api):
        value = None if name.startswith("_") else getattr(api, name, None)
        if inspect.ismethod(value) or inspect.isfunction(value):
            names.append(name)
    return sorted(names)


def bridge_script(names: list[str]) -> str:
    """The user script defining `window.pywebview.api`, then dispatching `pywebviewready`.

    An async function turns a message WebKit cannot send into a rejected Promise, never a throw, so the
    page's `.catch()` still applies.
    """
    return f"""(() => {{
    "use strict";
    if (window.pywebview !== undefined) return;
    const api = {{}};
    for (const method of {json.dumps(names)}) {{
        api[method] = async (...args) =>
            window.webkit.messageHandlers.{_HANDLER}.postMessage({{ method, args }});
    }}
    window.pywebview = Object.freeze({{ api: Object.freeze(api) }});
    const ready = () => window.dispatchEvent(new Event("pywebviewready"));
    if (document.readyState === "loading") {{
        document.addEventListener("DOMContentLoaded", ready, {{ once: true }});
    }} else {{
        ready();
    }}
}})();
"""


@dataclass(eq=False)
class _Call:
    """A call waiting for its answer. WebKit answers `undefined` when `reply` is released unanswered."""

    reply: Any
    context: Any  # the JavaScriptCore context of the posted message, which holds the result


class ScriptBridge:
    """Run the page's API calls on a thread pool and answer each once, on GTK's main loop."""

    def __init__(
        self, api: object, idle_add: Callable[[Callable[[], bool]], object], *, workers: int = 16
    ) -> None:
        self.api = api
        self.names = methods(api)
        self._exposed = frozenset(self.names)
        self._idle_add = idle_add
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="page-api")
        self._lock = threading.Lock()
        self._pending: set[_Call] = set()
        self._closed = False

    def attach(self, manager: Any) -> None:
        """On GTK's main loop, before the page loads: the message handler, its signal and the script."""
        webkit = importlib.import_module("gi.repository.WebKit")
        if not manager.register_script_message_handler_with_reply(_HANDLER, None):
            raise RuntimeError(f"A script message handler named {_HANDLER} is registered already")
        manager.connect(f"script-message-with-reply-received::{_HANDLER}", self.received)
        manager.add_script(
            webkit.UserScript.new(
                bridge_script(self.names),
                webkit.UserContentInjectedFrames.TOP_FRAME,
                webkit.UserScriptInjectionTime.START,
                None,
                None,
            )
        )

    def received(self, manager: Any, value: Any, reply: Any) -> bool:
        """WebKit's signal, on GTK's main loop. True: this handler answers, now or after the call."""
        try:
            method, args = self._parse(value.to_json(0))
        except ValueError as error:
            reply.return_error_message(str(error))
            return True
        # Keeping the reply after the signal returns defers the answer: PyGObject takes a reference to it.
        call = _Call(reply, value.get_context())
        with self._lock:
            if not self._closed:
                self._pending.add(call)
                self._pool.submit(self._run, call, method, args)
                return True
        reply.return_error_message(_CLOSING)
        return True

    def dispatch(self, method: str, args: list[Any]) -> Any:
        """The call itself, on the calling thread: an API method with the page's arguments."""
        return self._method(method)(*args)

    def close(self) -> None:
        """Refuse new calls and answer waiting ones with an error; running calls are not waited for."""
        with self._lock:
            self._closed = True
            waiting = list(self._pending)
        for call in waiting:
            self._idle_add(functools.partial(self._answer, call, None, _CLOSING))
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _method(self, name: str) -> Callable[..., Any]:
        if name not in self._exposed:
            raise ValueError(f"The window has no method {name!r}")
        method: Callable[..., Any] = getattr(self.api, name)
        return method

    def _parse(self, text: str | None) -> tuple[str, list[Any]]:
        """The method and arguments of a posted message; ValueError says why it is refused.

        `text` is None for a message JSON cannot represent, such as undefined or a cycle.
        """
        try:
            message = json.loads(text) if text is not None else None
        except (ValueError, RecursionError):
            message = None
        if not (
            isinstance(message, dict)
            and isinstance(method := message.get("method"), str)
            and isinstance(args := message.get("args"), list)
        ):
            raise ValueError("A window call needs a method name and a list of arguments")
        self._method(method)
        return method, args

    def _run(self, call: _Call, method: str, args: list[Any]) -> None:
        """On a worker thread: make the call, then answer it on GTK's main loop."""
        result: str | None = None
        error: str | None = None
        try:
            # Strict JSON: JavaScriptCore refuses NaN and Infinity.
            result = json.dumps(self.dispatch(method, args), allow_nan=False)
        except Exception as exc:
            _LOG.exception("The page's call to %s failed", method)
            # WebKit takes UTF-8, which a lone surrogate (from an undecodable file name) is not.
            error = (str(exc) or type(exc).__name__).encode("utf-8", "replace").decode()
        self._idle_add(functools.partial(self._answer, call, result, error))

    def _answer(self, call: _Call, result: str | None, error: str | None) -> bool:
        """On GTK's main loop: answer the call unless it has its answer already. False: run once."""
        with self._lock:
            if call not in self._pending:
                return False
            self._pending.remove(call)
        try:
            if error is not None:
                call.reply.return_error_message(error)
            else:
                javascript = importlib.import_module("gi.repository.JavaScriptCore")
                call.reply.return_value(javascript.Value.new_from_json(call.context, result))
        except Exception:
            _LOG.exception("Could not answer the page's call")
        return False
