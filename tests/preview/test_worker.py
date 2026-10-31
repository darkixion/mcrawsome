"""PreviewWorker: a preview session in its own process, newest request first."""

import multiprocessing
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from mcrawsome.preview.worker import PreviewWorker
from mcrawsome.recipe.load import build_recipe

STEP = 41_666_667


@pytest.fixture
def clip(make_mcraw: Callable[..., Path], tmp_path: Path) -> Path:
    folder = tmp_path / "clips"
    folder.mkdir()
    return make_mcraw(folder / "clip.mcraw", [i * STEP for i in range(6)])


@pytest.fixture
def worker(clip: Path) -> Iterator[PreviewWorker]:
    with PreviewWorker(clip, build_recipe([{"develop": {"exposure": 0.0}}])) as opened:
        yield opened


def _replies(
    worker: PreviewWorker, until: Callable[[dict[str, Any]], bool], timeout: float = 60.0
) -> list[tuple[dict[str, Any], bytes]]:
    """Replies until one satisfies `until` (included)."""
    got: list[tuple[dict[str, Any], bytes]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = worker.receive(0.1)
        if reply is None:
            continue
        got.append(reply)
        if until(reply[0]):
            return got
    raise AssertionError(f"no such reply within {timeout} s; got {[header for header, _ in got]}")


def _is(header: dict[str, Any], kind: str, seq: int) -> bool:
    return bool(header["kind"] == kind and header.get("seq") == seq)


def _mean(jpeg: bytes) -> float:
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert image is not None
    return float(np.mean(image))


def test_the_worker_says_what_it_opened(worker: PreviewWorker) -> None:
    assert worker.frame_count == 6
    assert worker.size == (64, 32)
    assert worker.fps == pytest.approx(24.0, rel=1e-3)


def test_only_the_newest_of_three_frame_requests_is_delivered(worker: PreviewWorker) -> None:
    for seq, index in ((1, 0), (2, 3), (3, 5)):
        worker.send({"seq": seq, "op": "frame", "index": index})
    replies = _replies(worker, lambda header: header["kind"] == "refined")
    assert [header["seq"] for header, _ in replies] == [3, 3]
    assert [header["kind"] for header, _ in replies] == ["draft", "refined"]
    draft, refined = replies
    assert draft[0]["index"] == refined[0]["index"] == 5
    assert draft[0]["ms"] >= 0
    assert refined[0]["notes"] == [
        "not stabilised: no gyro log (clip.gcsv) next to the clip, and no motion recorded in it"
    ]
    assert draft[1][:2] == refined[1][:2] == b"\xff\xd8"


def test_a_recipe_change_while_a_frame_develops_gives_the_new_exposure(worker: PreviewWorker) -> None:
    worker.send({"seq": 1, "op": "frame", "index": 2})
    [(_, dark)] = _replies(worker, lambda header: header["kind"] == "draft")
    worker.send({"seq": 2, "op": "frame", "index": 4})
    worker.send({"seq": 3, "op": "recipe", "recipe": build_recipe([{"develop": {"exposure": 2.0}}])})
    replies = _replies(worker, lambda header: header["kind"] == "draft")
    assert [header["seq"] for header, _ in replies] == [3]
    header, bright = replies[0]
    assert header["index"] == 4
    assert header["exposure"] == {"auto": False, "stops": 2.0, "measuring": False}
    assert _mean(bright) > _mean(dark) + 10


def test_a_refined_frame_is_not_sent_when_a_newer_request_came(worker: PreviewWorker) -> None:
    worker.send({"seq": 1, "op": "frame", "index": 1})
    _replies(worker, lambda header: header["kind"] == "draft")
    time.sleep(0.1)  # before the refined frame is due
    worker.send({"seq": 2, "op": "frame", "index": 2})
    replies = _replies(worker, lambda header: header["kind"] == "refined")
    assert [(header["seq"], header["kind"]) for header, _ in replies] == [(2, "draft"), (2, "refined")]


def test_refine_asks_for_the_refined_frame_at_once(worker: PreviewWorker) -> None:
    worker.send({"seq": 1, "op": "refine", "index": 3})
    replies = _replies(worker, lambda header: header["kind"] == "refined")
    assert [(header["seq"], header["index"]) for header, _ in replies] == [(1, 3)]


def test_a_bad_request_is_answered_with_an_error(worker: PreviewWorker) -> None:
    worker.send({"seq": 1, "op": "frame", "index": 99})
    [(header, payload)] = _replies(worker, lambda header: header["kind"] == "error")
    assert header["seq"] == 1
    assert header["error"]["code"] == "recipe-invalid"
    assert "outside" in header["error"]["message"]
    assert payload == b""
    worker.send({"seq": 2, "op": "frame", "index": 1})  # the worker goes on
    _replies(worker, lambda header: header["kind"] == "draft")


def test_the_frame_before_the_display_rendering_is_given(worker: PreviewWorker) -> None:
    worker.send({"seq": 1, "op": "frame", "index": 1})
    _replies(worker, lambda header: header["kind"] == "draft")
    worker.send({"seq": 2, "op": "linear16"})
    replies = _replies(worker, lambda header: header["kind"] == "linear16")
    header, frame = replies[-1]
    assert (header["width"], header["height"], header["index"]) == (64, 32, 1)
    assert len(frame) == 64 * 32 * 3 * 2


def test_an_automatic_exposure_is_measured_in_the_background(clip: Path) -> None:
    with PreviewWorker(clip, build_recipe([{"develop": {"exposure": "auto"}}])) as worker:
        worker.send({"seq": 1, "op": "frame", "index": 1})
        replies = _replies(worker, lambda header: not header["exposure"]["measuring"])
    first, last = replies[0][0], replies[-1][0]
    assert first["kind"] == "draft"
    assert first["exposure"] == {"auto": True, "stops": None, "measuring": True}
    assert last["kind"] == "draft"  # sent again once measured
    assert last["seq"] == 1
    assert last["exposure"]["auto"] is True
    assert last["exposure"]["stops"] not in (None, 0.0)


def test_closing_the_worker_leaves_no_process(clip: Path) -> None:
    worker = PreviewWorker(clip, build_recipe([]))
    worker.send({"seq": 1, "op": "frame", "index": 0})
    worker.close()
    assert multiprocessing.active_children() == []
    worker.close()  # twice is harmless


def test_a_spare_process_opens_the_next_clip_itself(clip: Path) -> None:
    from mcrawsome.preview.worker import Spare

    spare = Spare(gpu=False)
    started = spare._process.pid
    with PreviewWorker(clip, build_recipe([]), spare=spare) as worker:
        assert worker._process.pid == started  # no new process
        assert worker.frame_count == 6
        worker.send({"seq": 1, "op": "frame", "index": 0, "fast": True})
        _replies(worker, lambda header: _is(header, "draft", 1))
    assert not spare.alive
    assert multiprocessing.active_children() == []


def test_an_unused_spare_closes_and_a_dead_one_is_not_used(clip: Path) -> None:
    from mcrawsome.preview.worker import Spare

    unused = Spare(gpu=False)
    unused.close()
    unused.close()  # twice is harmless
    assert multiprocessing.active_children() == []
    dead = Spare(gpu=False)
    dead._process.kill()
    dead._process.join()
    with PreviewWorker(clip, build_recipe([]), spare=dead) as worker:  # a process of its own instead
        assert worker._process.pid != dead._process.pid
        assert worker.frame_count == 6
    assert multiprocessing.active_children() == []


def test_a_clip_that_cannot_be_opened_is_refused(tmp_path: Path) -> None:
    from mcrawsome.errors import McError

    with pytest.raises(McError, match="does not exist") as caught:
        PreviewWorker(tmp_path / "missing.mcraw", build_recipe([]))
    assert caught.value.code == "input-missing"
    assert multiprocessing.active_children() == []


def test_closing_a_worker_removes_the_shared_memory_its_process_left(clip: Path) -> None:
    """A refined frame of a video holds a shared-memory ring; a preview stopped while it works cannot
    remove it, so closing does."""
    from multiprocessing import shared_memory

    from mcrawsome.pipeline.shared import block_prefix

    worker = PreviewWorker(clip, build_recipe([]))
    pid = worker._process.pid
    assert pid is not None
    left = shared_memory.SharedMemory(name=f"{block_prefix(pid)}99", create=True, size=4096)
    left.close()  # as a killed process leaves it: still named in /dev/shm
    worker.close()
    assert not list(Path("/dev/shm").glob(f"{block_prefix(pid)}*"))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process groups and named semaphore files")
def test_forced_preview_close_reclaims_its_pool_semaphores(clip: Path) -> None:
    """An unresponsive refinement pool must leave neither resources nor tracker warnings."""
    program = """
import multiprocessing as mp
import os
from pathlib import Path
import signal
import sys
import time
import mcrawsome.preview.worker as module
from mcrawsome.recipe.load import build_recipe
from mcrawsome.preview.session import PreviewSettings

module.CLOSE_S = 0.01
foreign = mp.get_context('spawn').Semaphore()
foreign_path = Path('/dev/shm/sem.' + foreign._semlock.name.lstrip('/'))
worker = module.PreviewWorker(
    Path(sys.argv[1]), build_recipe([{'develop': {'exposure': 0}}]), PreviewSettings(gpu=False)
)
pid = worker._process.pid
prefix = f'sem.mcrawsome_p{pid}-'
stopped = False
try:
    worker.send({'seq': 1, 'op': 'refine', 'index': 0})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if list(Path('/dev/shm').glob(prefix + '*')):
            os.killpg(pid, signal.SIGSTOP)
            stopped = True
            break
        time.sleep(.001)
    assert stopped, 'refinement pool did not start'
    owned = list(Path('/dev/shm').glob(prefix + '*'))
    assert owned, 'refinement semaphores have no preview ownership prefix'
    worker.close()
    stopped = False
    assert not any(path.exists() for path in owned), 'forced close left named semaphores'
    assert foreign_path.exists(), 'another process owner lost its semaphore'
    assert foreign.acquire(timeout=1)
    foreign.release()
finally:
    if stopped:
        os.killpg(pid, signal.SIGCONT)
    worker.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(clip)], capture_output=True, text=True, timeout=35
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == "", result.stderr


def test_failed_measured_draft_drops_old_refinement_and_delivers_a_fresh_one(
    clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import Future

    from mcrawsome.preview.session import PreviewSession, Refined
    from mcrawsome.preview.worker import Reply, _Loop

    parent, child = multiprocessing.Pipe()
    with PreviewSession(clip, build_recipe([])) as session:
        loop = _Loop(child, session)
        replies: list[Reply] = []

        def send(reply: Reply, seq: int | None = None) -> bool:
            replies.append(reply)
            return True

        monkeypatch.setattr(loop, "_send", send)
        try:
            loop.newest = 1
            loop._draft(1, time.perf_counter())
            key, _ = session.auto_measurer() or ("", lambda: 0.0)
            assert key
            measured: Future[float] = Future()
            measured.set_result(1.0)
            old: Future[Refined] = Future()
            old.set_result(Refined(b"stale", []))
            loop.refine_due = None
            loop.measuring = (measured, key)
            loop.refining = (old, 1, 0, time.perf_counter())
            loop._refining_revision = loop._draft_revision
            draft = loop._draft

            def failing(seq: int, received: float) -> None:
                raise ValueError("measured draft failed")

            monkeypatch.setattr(loop, "_draft", failing)
            loop._background()
            assert [header["kind"] for header, _ in replies] == ["draft", "error"]
            monkeypatch.setattr(loop, "_draft", draft)
            loop._draft(1, time.perf_counter())
            loop.refine_due = (0.0, 1, 0)
            loop._background()
            assert loop.refining is not None
            loop.refining[0].result(timeout=10)
            loop._background()
            assert [header["kind"] for header, _ in replies] == ["draft", "error", "draft", "refined"]
            assert replies[-1][1] != b"stale"
        finally:
            loop.background.shutdown(wait=True, cancel_futures=True)
            parent.close()
            child.close()


def test_first_draft_arrives_while_measurement_and_refinement_are_blocked(
    clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from mcrawsome.preview.session import PreviewSession, Refined
    from mcrawsome.preview.worker import _Loop

    release = threading.Event()
    measuring, refining = threading.Event(), threading.Event()

    def measure() -> float:
        measuring.set()
        assert release.wait(10)
        return 1.0

    def refine() -> Refined:
        refining.set()
        assert release.wait(10)
        return Refined(b"refined")

    parent, child = multiprocessing.Pipe()
    with PreviewSession(clip, build_recipe([])) as session:
        monkeypatch.setattr(session, "auto_measurer", lambda **kwargs: ("pending", measure))
        monkeypatch.setattr(session, "refiner", lambda index: refine)
        loop = _Loop(child, session)
        try:
            loop._answer([{"seq": 1, "op": "frame", "index": 0}], time.perf_counter())
            assert not measuring.is_set()
            loop.measure_due = 0.0
            loop._background()
            assert measuring.wait(2)
            loop.refine_due = (0.0, 1, 0)
            loop._background()
            assert refining.wait(2)
            assert parent.poll(0.1)
            header, jpeg = parent.recv()
            assert header["kind"] == "draft"
            assert jpeg.startswith(b"\xff\xd8")
            assert not release.is_set()
        finally:
            release.set()
            loop.close()
            parent.close()
            child.close()


def test_changing_output_rate_updates_the_preview_clock_and_keeps_position(worker: PreviewWorker) -> None:
    worker.send({"seq": 1, "op": "frame", "index": 5})
    _replies(worker, lambda h: _is(h, "draft", 1))
    worker.send(
        {
            "seq": 2,
            "op": "recipe",
            "index": 5,
            "recipe": build_recipe([{"develop": {"exposure": 0.0}, "output": {"frame_rate": 12}}]),
        }
    )
    header, _ = _replies(worker, lambda h: _is(h, "draft", 2))[-1]
    assert header["frame_count"] == 3
    assert header["rate"] == 12
    assert header["index"] == 2
    assert worker.frame_count == 3
    assert worker.rate == 12


@pytest.mark.parametrize("gpu", [False, True])
def test_driver_discovery_finishes_before_session_readers_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gpu: bool
) -> None:
    import os
    import signal

    from mcrawsome.preview import worker as module
    from mcrawsome.preview.session import PreviewSettings

    events: list[str] = []
    monkeypatch.setenv("MCRAWSOME_PREVIEW_GPU", "1")
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(os, "setpgrp", lambda: None)
    config = multiprocessing.current_process()._config  # type: ignore[attr-defined]
    monkeypatch.setitem(config, "semprefix", config["semprefix"])
    monkeypatch.setattr(module, "prepare_runtime", lambda: events.append("drivers discovered"))

    def open_session(*args: Any) -> Any:
        events.append("session readers")
        raise OSError("fixture stopped before opening")

    monkeypatch.setattr(module, "PreviewSession", open_session)
    parent, child = multiprocessing.Pipe()
    cache_parent, cache_child = multiprocessing.Pipe()
    try:
        module._serve(
            child,
            tmp_path / "unused.mcraw",
            build_recipe([]),
            PreviewSettings(gpu=gpu),
            multiprocessing.RawValue("q", 0),
            cache_child,
        )
        assert parent.recv()[0]["kind"] == "error"
        assert events == (["drivers discovered"] if gpu else []) + ["session readers"]
    finally:
        for connection in (parent, child, cache_parent, cache_child):
            connection.close()
