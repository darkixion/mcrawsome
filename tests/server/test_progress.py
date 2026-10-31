"""The stages of a send, from the engine's work to the page's event stream."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from fastapi import Request
from starlette.types import Message

from mcrawsome.server.progress import MAX_PENDING, ProgressHub


def test_stages_reach_every_open_stream_until_it_closes() -> None:
    hub = ProgressHub()
    first, second = hub.subscribe(), hub.subscribe()
    hub.publish({"stage": "Reading its gyro"})
    hub.unsubscribe(second)
    hub.publish({"stage": "Writing the timeline"})
    assert [first.get_nowait(), first.get_nowait()] == [
        {"stage": "Reading its gyro"},
        {"stage": "Writing the timeline"},
    ]
    assert second.get_nowait() == {"stage": "Reading its gyro"}
    assert second.empty()


def test_a_stream_that_falls_behind_keeps_the_latest_stages() -> None:
    hub = ProgressHub()
    stages = hub.subscribe()
    for index in range(MAX_PENDING + 3):
        hub.publish({"stage": str(index)})
    kept = [stages.get_nowait()["stage"] for _ in range(stages.qsize())]
    assert kept == [str(index) for index in range(3, MAX_PENDING + 3)]


def test_send_stages_reach_the_event_stream(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mcrawsome.server.app as module
    from mcrawsome.library.scan import Library
    from mcrawsome.library.watch import LibraryWatcher

    monkeypatch.setattr(module, "EVENT_POLL_S", 0.001)
    hub = ProgressHub()
    watcher = LibraryWatcher(Library(), lambda: [library])
    stage = {"send": "page-1", "mount": "mount-" + "a" * 24, "stage": "Tracking image detail"}

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def follow() -> None:
        stream = module._events(watcher, Request({"type": "http"}, receive), threading.Event(), progress=hub)
        try:
            assert await anext(stream) == ": connected\n\n"
            hub.publish(stage)
            event = await asyncio.wait_for(anext(stream), 2)
            assert event == f"event: send\ndata: {json.dumps(stage)}\n\n"
        finally:
            await stream.aclose()

    asyncio.run(follow())
    assert not hub._subscribers  # a closed page no longer collects stages
