"""Cancellation owns and reaps the child even before its session has opened."""

import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from mcrawsome.errors import McError
from mcrawsome.preview import worker as module
from mcrawsome.recipe.schema import Recipe


def slow_start(conn: Any, path: Path, *args: Any) -> None:
    os.setpgrp()
    path.write_text(str(os.getpid()))
    time.sleep(60)  # the parent must cancel this, rather than waiting for the startup timeout


@pytest.mark.timeout(15)
def test_cancellation_during_real_child_startup_leaves_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "started"
    cancelled = threading.Event()
    monkeypatch.setattr(module, "_serve", slow_start)

    def cancel() -> None:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancelled.set()

    cancelling = threading.Thread(target=cancel)
    cancelling.start()
    try:
        with pytest.raises(McError) as caught:
            module.PreviewWorker(marker, Recipe(), cancelled=cancelled)
        assert caught.value.code == "cancelled"
        assert marker.exists()
        assert multiprocessing.active_children() == []
    finally:
        cancelling.join()


def test_already_cancelled_open_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any) -> None:
        pytest.fail("a cancelled opening must not spawn")

    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(multiprocessing, "get_context", forbidden)
    with pytest.raises(McError) as caught:
        module.PreviewWorker(Path("unused"), Recipe(), cancelled=cancelled)
    assert caught.value.code == "cancelled"


def test_a_cancelled_open_closes_the_spare_it_was_given() -> None:
    closed: list[bool] = []

    class Spare:
        def close(self) -> None:
            closed.append(True)

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(McError):
        module.PreviewWorker(Path("unused"), Recipe(), cancelled=cancelled, spare=Spare())  # type: ignore[arg-type]
    assert closed == [True]


@pytest.mark.parametrize("alive", [False, True])
def test_signalling_before_setpgrp_targets_only_the_owned_child(
    monkeypatch: pytest.MonkeyPatch, alive: bool
) -> None:
    class Process:
        pid = 123456789

        def is_alive(self) -> bool:
            return alive

    sent: list[tuple[int, signal.Signals]] = []

    def no_group(*args: Any) -> None:
        raise ProcessLookupError

    worker = module.PreviewWorker.__new__(module.PreviewWorker)
    monkeypatch.setattr(worker, "_process", Process(), raising=False)
    monkeypatch.setattr(os, "killpg", no_group)
    monkeypatch.setattr(os, "kill", lambda pid, stop: sent.append((pid, stop)))
    worker._signal_group(signal.SIGTERM)
    assert sent == ([(123456789, signal.SIGTERM)] if alive else [])
