import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import pytest

from mcrawsome.preview.session import PreviewSession, PreviewSettings, Refined
from mcrawsome.preview.worker import _Loop
from mcrawsome.recipe.load import build_recipe


@pytest.mark.parametrize("activity", ["playback", "scrub"])
def test_interactive_drafts_do_not_start_clip_measurement(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch, activity: str
) -> None:
    parent, child = multiprocessing.Pipe()
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        calls: list[bool] = []

        def measurer(**kwargs: Any) -> tuple[str, Callable[[], float]]:
            calls.append(True)
            return "test", lambda: 1.0

        monkeypatch.setattr(session, "auto_measurer", measurer)
        loop = _Loop(child, session)
        try:
            loop._answer(
                [{"seq": 1, "op": "frame", "index": 0, "fast": True, activity: True}],
                time.perf_counter(),
            )
            loop._background()
            assert parent.poll(1)
            assert parent.recv()[0]["kind"] == "draft"
            assert calls == []
            assert loop.measuring is None
        finally:
            loop.close()
            parent.close()
            child.close()


def test_idle_measurement_waits_for_inputs_to_settle(tiny_raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, child = multiprocessing.Pipe()
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        calls: list[bool] = []

        def measurer(**kwargs: Any) -> tuple[str, Callable[[], float]]:
            calls.append(True)
            return "test", lambda: 1.0

        monkeypatch.setattr(session, "auto_measurer", measurer)
        monkeypatch.setattr(session, "refiner", lambda index: lambda: Refined(b"test"))
        loop = _Loop(child, session)
        try:
            loop._answer([{"seq": 1, "op": "frame", "index": 0, "fast": True}], time.perf_counter())
            loop._background()
            assert calls == []
            loop.measure_due = 0.0
            loop._background()
            assert calls == [True]
        finally:
            loop.close()
            parent.close()
            child.close()


@pytest.mark.parametrize("failed_after_cancel", [False, True])
def test_playback_cancels_running_measurement_without_an_error(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch, failed_after_cancel: bool
) -> None:
    parent, child = multiprocessing.Pipe()
    release = threading.Event()
    started = threading.Event()
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:

        def measurer(*, cancelled: Callable[[], bool] = lambda: False) -> tuple[str, Callable[[], float]]:
            def measure() -> float:
                started.set()
                assert release.wait(2)
                if cancelled():
                    if failed_after_cancel:
                        raise ValueError("superseded measurement failed")
                    raise CancelledError()
                return 1.0

            return session.auto_key() or "test", measure

        monkeypatch.setattr(session, "auto_measurer", measurer)
        loop = _Loop(child, session)
        try:
            loop.measure_due = 0.0
            loop._measure()
            assert started.wait(2)
            assert loop.measuring is not None
            task = loop.measuring[0]
            loop._answer(
                [{"seq": 1, "op": "frame", "index": 0, "fast": True, "playback": True}],
                time.perf_counter(),
            )
            release.set()
            with pytest.raises(ValueError if failed_after_cancel else CancelledError):
                task.result(timeout=2)
            loop._background()
            assert loop.auto_error is None
            assert loop.measuring is None
            assert session.auto_pending
        finally:
            release.set()
            loop.close()
            parent.close()
            child.close()


def test_auto_measurement_checks_cancellation_between_frames(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis.resolve import Resolution
    from mcrawsome.preview import session as module
    from mcrawsome.progress import Progress

    cancelled = threading.Event()

    def resolve(recipe: Any, job: Any, count: int, workers: int, *, progress: Progress) -> Resolution:
        assert workers == 1
        cancelled.set()
        progress("measuring", 1, count)
        raise AssertionError("measurement continued after cancellation")

    monkeypatch.setattr(module, "resolve_auto", resolve)
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        measurer = session.auto_measurer(cancelled=cancelled.is_set)
        assert measurer is not None
        with pytest.raises(CancelledError):
            measurer[1]()


def test_gpu_playback_uses_one_device_owner_and_overlaps_raw_reading(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.preview.backend import PreviewBackend

    monkeypatch.setenv("MCRAWSOME_PREVIEW_GPU", "1")
    monkeypatch.setattr(PreviewBackend, "prepare", lambda self: None)
    monkeypatch.setattr(PreviewBackend, "_device", lambda self: None)
    calls: list[tuple[int, int, bool]] = []
    priorities: list[int] = []
    priority = (
        os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) if hasattr(os, "getpriority") else None
    )
    original = PreviewSession.fast

    def fast(self: PreviewSession, index: int, width: int = 1280, **kwargs: Any) -> Refined:
        calls.append((id(self.backend), threading.get_ident(), kwargs.get("prefetch", False)))
        if priority is not None:
            priorities.append(os.getpriority(os.PRIO_PROCESS, threading.get_native_id()))
        return original(self, index, width, **kwargs)

    monkeypatch.setattr(PreviewSession, "fast", fast)
    parent, child = multiprocessing.Pipe()
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=True)) as session:
        loop = _Loop(child, session)
        try:
            loop._answer(
                [{"seq": 1, "op": "frame", "index": 0, "fast": True, "playback": True, "ahead": 3}],
                time.perf_counter(),
            )
            assert len(calls) == 3
            assert len({backend for backend, _, _ in calls}) == 1
            assert len({thread for _, thread, _ in calls}) == 1
            assert all(prefetch for _, _, prefetch in calls)
            assert all(value == priority for value in priorities)
            assert [parent.recv()[0]["index"] for _ in range(3)] == [0, 1, 2]
        finally:
            loop.close()
            parent.close()
            child.close()
