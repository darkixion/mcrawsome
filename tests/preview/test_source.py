"""Source-size development and bounded playback share the interactive device owner."""

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from mcrawsome import _raw
from mcrawsome.preview.backend import PreviewBackend
from mcrawsome.preview.prefetch import PlaybackStream
from mcrawsome.preview.session import PreviewSession, PreviewSettings, Refined
from mcrawsome.recipe.load import build_recipe


def test_source_size_ignores_draft_limit_and_reuses_finished_frames(tiny_raw: Path) -> None:
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    with PreviewSession(tiny_raw, recipe, PreviewSettings(width=32, gpu=False)) as session:
        encode = session.source_encoder(1)
        assert session.cache_bytes == 0
        frame = encode()
        assert frame.size == (128, 64)
        image = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert image is not None
        assert image.shape == (64, 128, 3)
        assert session.source_encoder(1)() is frame
        session.set_recipe(build_recipe([{"develop": {"exposure": 1}}]), fast=True)
        assert session.source_encoder(1)().jpeg != frame.jpeg
        assert session.cache_bytes <= session.settings.cache_mib * 1024**2


def test_source_producer_obeys_clock_credit_and_skips_obsolete_work(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(10)])
    rendered: list[int] = []

    def source(self: PreviewSession, index: int, **kwargs: Any) -> Callable[[], Refined]:
        rendered.append(index)
        return lambda: Refined(str(index).encode(), size=(128, 64))

    monkeypatch.setattr(PreviewSession, "source_encoder", source)
    with PreviewSession(clip, build_recipe([]), PreviewSettings(gpu=False)) as session:
        stream = session.pipeline.stream(0)
        for index in range(3):
            found = stream.receive(timeout=2)
            assert found is not None
            assert found[0] == index
        assert stream.receive(timeout=0.03) is None
        assert rendered == [0, 1, 2]
        stream.advance(7)
        for index in range(7, 10):
            found = stream.receive(timeout=2)
            assert found is not None
            assert found[0] == index
        assert rendered == [0, 1, 2, 7, 8, 9]


def test_only_a_late_slow_frame_moves_the_aim_ahead_of_the_clock() -> None:
    from types import SimpleNamespace

    from mcrawsome.preview.prefetch import PlaybackStream

    lane = SimpleNamespace(cancelled=threading.Event())
    stream = PlaybackStream(lane, SimpleNamespace(frame_count=100, rate=24.0), 10)  # type: ignore[arg-type]
    stream.pace(12, 1.2)  # slower than a frame, but done before the clock reached it: no frame skipped
    assert (stream.lead, stream.window) == (0, 3)
    stream.pace(10, 3.5)  # slow and late: aim further ahead
    assert (stream.lead, stream.window) == (5, 7)
    stream.pace(14, 1.2)  # slow but early: the aim may come back, never go further
    assert (stream.lead, stream.window) == (3, 5)
    stream.pace(15, 2.5)
    assert stream.lead == 3
    stream.pace(10, 40.0)  # late after a long stall: half a second at most
    assert stream.lead == 12
    stream.pace(16, 0.8)  # a frame within its period needs no aim
    assert (stream.lead, stream.window) == (0, 3)


def test_source_producer_overlaps_encoding_and_retains_one_gpu_owner(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_PREVIEW_GPU", "1")
    monkeypatch.setattr(PreviewBackend, "prepare", lambda self: None)
    release, second = threading.Event(), threading.Event()
    calls: list[tuple[int, int]] = []
    encode_threads: list[int] = []

    def source(self: PreviewSession, index: int, **kwargs: Any) -> Callable[[], Refined]:
        calls.append((id(self.backend), threading.get_ident()))
        if index == 1:
            second.set()

        def encode() -> Refined:
            encode_threads.append(threading.get_ident())
            if index == 0:
                assert release.wait(2)
            return Refined(b"jpeg", size=(128, 64))

        return encode

    monkeypatch.setattr(PreviewSession, "source_encoder", source)
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=True)) as session:
        stream = session.pipeline.stream(0)
        try:
            assert second.wait(2), "GPU work waited for JPEG encoding"
            release.set()
            for _ in range(3):
                assert stream.receive(timeout=2) is not None
            assert len(set(calls)) == 1
            assert set(encode_threads).isdisjoint({thread for _, thread in calls})
            draft = session.pipeline.render_source(1)
            assert draft.frame.jpeg == b"jpeg"
            assert len(set(calls)) == 1, "a manual source seek changed GPU owner"
        finally:
            release.set()


def test_source_producer_cancels_with_a_full_output_queue(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(20)])
    prepared = threading.Event()
    rendered: list[int] = []

    def source(self: PreviewSession, index: int, **kwargs: Any) -> Callable[[], Refined]:
        rendered.append(index)
        if len(rendered) >= 3:
            prepared.set()
        return lambda: Refined(b"jpeg", size=(128, 64))

    monkeypatch.setattr(PreviewSession, "source_encoder", source)
    with PreviewSession(clip, build_recipe([]), PreviewSettings(gpu=False)) as session:
        stream = session.pipeline.stream(0)
        stream.advance(10)
        assert prepared.wait(2)
        session.cancel_pending()
        session.pipeline.stop_stream()
        assert stream.task is not None
        assert stream.task.done()
        assert len(rendered) <= 5, "output backpressure did not bound rendered frames"
        assert stream.receive() is None


def test_source_producer_reports_encoding_failure(tiny_raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def source(self: PreviewSession, index: int, **kwargs: Any) -> Callable[[], Refined]:
        def encode() -> Refined:
            raise ValueError("JPEG failed")

        return encode

    cancel = PlaybackStream.cancel

    def slow_cancel(self: PlaybackStream) -> None:
        cancel(self)
        time.sleep(0.5)  # the stream reads as stopped well before its task ends, as under load

    monkeypatch.setattr(PreviewSession, "source_encoder", source)
    monkeypatch.setattr(PlaybackStream, "cancel", slow_cancel)
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        stream = session.pipeline.stream(0)
        with pytest.raises(ValueError, match="JPEG failed"):
            stream.receive(timeout=2)


def test_source_snapshots_do_not_accumulate_cancellation_callbacks(tiny_raw: Path) -> None:
    from mcrawsome.preview.prefetch import _Lane

    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as owner:
        lane = _Lane()
        try:
            snapshot = owner.fork_fast()
            for _ in range(1100):
                lane.adopt(snapshot)
            assert lane.session is not None
            lane.session.backend.check_cancelled()
        finally:
            lane.close()


def test_cancelled_stream_cannot_reappear_after_another_seek(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        PreviewSession,
        "source_encoder",
        lambda self, index, **kwargs: lambda: Refined(b"jpeg", size=(128, 64)),
    )
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        stream = session.pipeline.stream(0)
        assert stream.receive(timeout=2) is not None
        session.pipeline.render_source(2)
        assert stream.receive() is None


def test_source_stream_clock_updates_do_not_cancel_and_manual_seek_does(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from multiprocessing.connection import Connection
    from typing import cast

    from mcrawsome.preview.prefetch import Draft, _Lane
    from mcrawsome.preview.worker import _Loop

    # Slow frames deliberately skip ahead: keep rendering time out of this protocol check.
    def source(self: _Lane, *args: Any, **kwargs: Any) -> Callable[[], Draft]:
        return lambda: Draft(Refined(b"jpeg", size=(128, 64)), {"total_ms": 0}, "CPU", "test")

    monkeypatch.setattr(_Lane, "source", source)

    class ConnectionStub:
        def __init__(self) -> None:
            self.replies: list[Any] = []

        def poll(self, timeout: float) -> bool:
            return False

        def send(self, reply: Any) -> None:
            self.replies.append(reply)

    clip = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(6)])
    with PreviewSession(
        clip, build_recipe([{"develop": {"exposure": 0}}]), PreviewSettings(gpu=False)
    ) as session:
        connection = ConnectionStub()
        loop = _Loop(cast(Connection, connection), session)
        try:
            loop._answer(
                [{"seq": 1, "op": "frame", "index": 0, "source": True, "playback": True}], time.perf_counter()
            )
            deadline = time.monotonic() + 2
            while len(connection.replies) < 3 and time.monotonic() < deadline:
                loop._pump_source()
                time.sleep(0.002)
            assert [header["index"] for header, _ in connection.replies] == [0, 1, 2]
            for header, _ in connection.replies:
                assert (header["width"], header["height"]) == (128, 64)
                assert header["stream"] is True
                assert header["source"] is True
            stream = loop.source_stream
            loop._answer([{"seq": 2, "op": "advance", "stream_seq": 1, "index": 3}], time.perf_counter())
            assert loop.source_stream is stream
            assert loop.newest == 1
            deadline = time.monotonic() + 2
            while len(connection.replies) < 6 and time.monotonic() < deadline:
                loop._pump_source()
                time.sleep(0.002)
            assert [header["index"] for header, _ in connection.replies] == list(range(6))
            loop._answer([{"seq": 3, "op": "frame", "index": 1, "source": True}], time.perf_counter())
            assert loop.source_stream is None
            assert connection.replies[-1][0]["index"] == 1
            assert connection.replies[-1][0]["source"] is True
            assert stream is not None
            assert stream.receive() is None
        finally:
            loop.close()


def test_completed_jpeg_is_published_while_the_next_gpu_frame_is_running(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second, release = threading.Event(), threading.Event()

    def source(self: PreviewSession, index: int, **kwargs: Any) -> Callable[[], Refined]:
        if index == 1:
            second.set()
            assert release.wait(2)
        return lambda: Refined(b"jpeg", size=(128, 64))

    monkeypatch.setattr(PreviewSession, "source_encoder", source)
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        stream = session.pipeline.stream(0)
        try:
            assert second.wait(2)
            found = stream.receive(timeout=0.05)
            assert found is not None, "finished JPEG waited for the following GPU frame"
            assert found[0] == 0
        finally:
            release.set()


def test_interactive_device_preparation_keeps_the_foreground_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from mcrawsome.preview import backend as module

    if not hasattr(os, "getpriority"):
        pytest.skip("Linux thread priority check")
    expected = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    observed: list[int] = []

    class Device:
        def __init__(self) -> None:
            observed.append(os.getpriority(os.PRIO_PROCESS, threading.get_native_id()))

        def close(self) -> None:
            pass

    monkeypatch.setattr(module, "OpenCL", Device)
    backend = PreviewBackend()
    try:
        backend.prepare()
        assert backend._preparing is not None
        backend._preparing.result(timeout=2)
        assert observed == [expected]
    finally:
        backend.close()


def test_a_streamed_draft_is_the_paused_draft_with_cpu_finishing_off_the_developing_thread(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    finishing: list[int] = []
    apply = _raw.apply_preview

    def recorded(*args: Any) -> Any:
        finishing.append(threading.get_ident())
        return apply(*args)

    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as paused:
        drafts = [paused.fast(index, 96) for index in range(3)]
        expected = [(draft.jpeg, draft.size) for draft in drafts]
    monkeypatch.setattr(_raw, "apply_preview", recorded)
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        stream = session.pipeline.stream(0, width=96)
        frames = []
        while True:
            found = stream.receive(timeout=20)
            if found is None:
                assert stream.task is not None
                assert stream.task.done()
                break
            frames.append(found)
            stream.advance(found[0] + 1)
        session.pipeline.stop_stream()
    indices = [index for index, _ in frames]
    assert indices[0] == 0
    assert indices[-1] == 2
    assert indices == sorted(set(indices))
    assert [(draft.frame.jpeg, draft.frame.size) for _, draft in frames] == [expected[i] for i in indices]
    assert finishing
    assert threading.get_ident() not in finishing
    assert all(name.startswith("preview-jpeg") for name in _thread_names(finishing))


def _thread_names(idents: list[int]) -> list[str]:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    return [names.get(ident, "preview-jpeg (ended)") for ident in idents]


def test_stream_keeps_a_completed_picture_when_processing_misses_the_audio_deadline(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    produced = threading.Event()

    def encode(self: PreviewSession, index: int, width: int, **kwargs: Any) -> Callable[[], Refined]:
        def finish() -> Refined:
            if index == 1:
                produced.set()
            return Refined(b"jpeg", size=(32, 16))

        return finish

    monkeypatch.setattr(PreviewSession, "draft_encoder", encode)
    with PreviewSession(tiny_raw, build_recipe([]), PreviewSettings(gpu=False)) as session:
        stream = session.pipeline.stream(0, width=32)
        assert produced.wait(5), (
            stream.task.exception() if stream.task and stream.task.done() else "producer stalled"
        )
        stream.advance(2)
        found = stream.receive(timeout=1)
        assert found is not None
        assert found[0] == 0
