"""Departing clients and server shutdown must not leave a preview still opening."""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mcrawsome.errors import McError
from mcrawsome.preview.session import PreviewSettings
from mcrawsome.recipe.schema import Recipe
from mcrawsome.server.routes import preview


def test_startup_failure_after_disconnect_is_quiet(
    client: TestClient, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted = threading.Event()
    clip = library / "failing.mcraw"
    clip.touch()

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise McError("cannot start", code="internal-error")

    async def disconnected(*args: Any, **kwargs: Any) -> None:
        attempted.set()
        raise WebSocketDisconnect(1006)

    monkeypatch.setattr(preview.PreviewHost, "open", fail)
    monkeypatch.setattr(WebSocket, "send_json", disconnected)
    with client.websocket_connect(f"ws://127.0.0.1:8642/api/v1/clips/local/preview?path={clip}"):
        assert attempted.wait(5)


@pytest.mark.parametrize("shutdown", [False, True])
def test_pending_constructor_is_cancelled_and_its_late_worker_closed(
    monkeypatch: pytest.MonkeyPatch, shutdown: bool
) -> None:
    entered, finish, closed, saw_cancel = (threading.Event() for _ in range(4))

    class Worker:
        def __init__(self, *args: Any, cancelled: threading.Event | None = None, **_: Any) -> None:
            entered.set()
            assert cancelled is not None
            if cancelled.wait(3):
                saw_cancel.set()
            assert finish.wait(3)

        def close(self) -> None:
            closed.set()

    monkeypatch.setattr(preview, "PreviewWorker", Worker)

    async def check() -> None:
        host = preview.PreviewHost()
        opening = asyncio.create_task(host.open(Path("clip"), Recipe(), PreviewSettings()))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            if shutdown:
                host.close()
            else:
                opening.cancel()
            assert await asyncio.to_thread(saw_cancel.wait, 3)
            finish.set()
            with pytest.raises((asyncio.CancelledError, McError)):
                await opening
            assert closed.is_set()
            assert not host._open
            assert host._current is None
        finally:
            finish.set()
            await asyncio.gather(opening, return_exceptions=True)
            host.close()

    asyncio.run(check())


def test_disconnect_during_open_cancels_constructor(
    client: TestClient, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = library / "starting.mcraw"
    clip.touch()
    entered, cancelled = threading.Event(), threading.Event()

    async def opening(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(preview.PreviewHost, "open", opening)
    with client.websocket_connect(f"ws://127.0.0.1:8642/api/v1/clips/local/preview?path={clip}") as ws:
        assert entered.wait(3)
        ws.close()
        assert cancelled.wait(3)


def test_connected_client_still_receives_a_real_startup_error(
    client: TestClient, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = library / "failing.mcraw"
    clip.touch()

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise McError("cannot start", code="internal-error")

    monkeypatch.setattr(preview.PreviewHost, "open", fail)
    with client.websocket_connect(f"ws://127.0.0.1:8642/api/v1/clips/local/preview?path={clip}") as ws:
        error = ws.receive_json()
        assert error["error"] == {"code": "internal-error", "message": "cannot start"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008


def test_an_early_frame_request_is_retained(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    clip = make_mcraw(library / "early.mcraw", [0, 41_666_667, 83_333_334])
    with client.websocket_connect(f"ws://127.0.0.1:8642/api/v1/clips/local/preview?path={clip}") as ws:
        ws.send_json({"seq": 1, "op": "recipe", "index": 1, "recipe": {"develop": {"exposure": 0}}})
        assert ws.receive_json()["kind"] == "opened"
        draft = ws.receive_json()
        assert (draft["kind"], draft["seq"], draft["index"]) == ("draft", 1, 1)
        assert ws.receive_bytes().startswith(b"\xff\xd8")


def test_cache_clear_during_a_clip_switch_never_targets_the_new_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, finish = threading.Event(), threading.Event()
    cleared: list[Path] = []

    class Worker:
        def __init__(self, path: Path, *args: Any, **kwargs: Any) -> None:
            self.path = path

        def cache_usage(self, *, clear: bool = False) -> dict[str, int]:
            if clear:
                cleared.append(self.path)
            entered.set()
            assert finish.wait(3)
            return {"bytes": 0}

        def close(self) -> None:
            pass

    monkeypatch.setattr(preview, "PreviewWorker", Worker)

    async def check() -> None:
        host = preview.PreviewHost()
        await host.open(Path("old"), Recipe(), PreviewSettings())
        operation = asyncio.create_task(host.cache_usage(clear=True))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await host.open(Path("new"), Recipe(), PreviewSettings())
            finish.set()
            with pytest.raises(McError, match="Preview changed"):
                await operation
            assert cleared == [Path("old")]
        finally:
            finish.set()
            await asyncio.gather(operation, *host._closing, return_exceptions=True)
            host.close()

    asyncio.run(check())
