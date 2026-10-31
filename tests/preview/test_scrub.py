"""Scrub caches stay bounded and warm both sides without changing the requested frame."""

import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from mcrawsome.develop.raw import Developer, Image
from mcrawsome.io.clip import Clip, RawFrame
from mcrawsome.preview.backend import PreviewBackend
from mcrawsome.preview.session import PreviewSession, PreviewSettings, Refined
from mcrawsome.preview.worker import Reply, _Loop
from mcrawsome.recipe.load import build_recipe


class _Connection:
    def __init__(self) -> None:
        self.replies: list[Reply] = []

    def poll(self, timeout: float) -> bool:
        return False

    def send(self, reply: Reply) -> None:
        self.replies.append(reply)


def test_fast_cache_revisits_without_reading_and_invalidates_development(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(8)])
    reads: list[int] = []
    original = Clip.raw_frame

    def read(self: Clip, index: int) -> RawFrame:
        reads.append(index)
        return original(self, index)

    monkeypatch.setattr(Clip, "raw_frame", read)
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    with PreviewSession(path, recipe, PreviewSettings(gpu=False)) as session:
        first = session.fast(2, width=32)
        session.fast(3, width=32)
        count = len(reads)
        assert session.fast(2, width=32) == first
        assert len(reads) == count
        assert session.fast_timings["cache_hit"] == 1
        session.set_recipe(build_recipe([{"develop": {"exposure": 1}}]), fast=True)
        assert session.fast(2, width=32).jpeg != first.jpeg
        assert len(reads) == count
        session.set_recipe(build_recipe([{"develop": {"exposure": 1, "white_level": 500}}]), fast=True)
        session.fast(2, width=32)
        assert len(reads) > count


def test_fast_cache_bound_and_disabled_cache(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(80)])
    for budget in (0, 1):
        with PreviewSession(path, build_recipe([]), PreviewSettings(cache_mib=budget, gpu=False)) as session:
            for index in range(session.frame_count):
                session.fast(index)
                assert session.cache_bytes <= budget * 1024**2
            assert (session.cache_bytes > 0) == bool(budget)
            session.close()
            assert session.cache_bytes == 0


def test_cache_eviction_and_oversized_frames() -> None:
    from mcrawsome.preview.cache import FrameCache

    cache = FrameCache[bytes](8)
    cache.put(0, b"one", 3)
    cache.put(1, b"two", 3)
    assert cache.get(0) == b"one"
    cache.put(2, b"three", 5)
    assert cache.get(1) is None
    assert cache.bytes == 8
    cache.put(0, b"oversized", 9)
    assert cache.get(0) is None
    assert cache.bytes == 5


def test_fast_reports_reader_stages(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False)) as session:
        session.fast(0)
        for stage in ("lookup_ms", "read_ms", "metadata_ms", "decompress_ms"):
            assert session.fast_timings[stage] >= 0


def test_playback_reads_the_next_raw_while_developing_this_frame(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667, 83_333_334])
    ready = threading.Event()
    reads: list[int] = []
    read = Clip.raw_frame
    develop = PreviewBackend.develop

    def reading(self: Clip, index: int) -> RawFrame:
        frame = read(self, index)
        reads.append(index)
        if index == 1:
            ready.set()
        return frame

    def developing(self: PreviewBackend, dev: Developer, raw: np.ndarray, step: int, width: int) -> Image:
        assert ready.wait(3), "RAW read-ahead did not overlap development"
        return develop(self, dev, raw, step, width)

    monkeypatch.setattr(Clip, "raw_frame", reading)
    monkeypatch.setattr(PreviewBackend, "develop", developing)
    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False)) as session:
        session.fast(0, prefetch=True)
        session.fast(1)
    assert reads.count(1) == 1


def test_idle_warms_neighbours_at_drag_and_step_sizes(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(12)])
    with PreviewSession(
        path, build_recipe([{"develop": {"exposure": 0}}]), PreviewSettings(gpu=False)
    ) as session:
        conn = _Connection()
        loop = _Loop(cast(Connection, conn), session)
        try:
            loop._answer([{"op": "frame", "seq": 1, "index": 6, "fast": True}], time.perf_counter())
            deadline = time.monotonic() + 5
            while loop.warming or session.pipeline.in_flight:
                assert time.monotonic() < deadline
                loop._warm()
                time.sleep(0.005)
            assert loop.index == 6
            assert len(conn.replies) == 1

            def unexpected(*args: object) -> None:
                pytest.fail("warmed neighbour read the clip")

            monkeypatch.setattr(Clip, "raw_frame", unexpected)
            for index in (5, 7):
                for width in (640, 1280):
                    session.fast(index, width=width)
                    assert session.fast_timings["cache_hit"] == 1
        finally:
            loop.close()


def test_scrub_drafts_are_small_until_an_exact_request_on_rest(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667, 83_333_334])
    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False)) as session:
        widths: list[tuple[int, int]] = []
        original = session.fast

        def fast(index: int, width: int = 1280, *, prefetch: bool = False, drag: bool = False) -> Refined:
            widths.append((index, width))
            return original(index, width, prefetch=prefetch, drag=drag)

        monkeypatch.setattr(session, "fast", fast)
        conn = _Connection()
        loop = _Loop(cast(Connection, conn), session)
        try:
            loop._answer(
                [{"op": "frame", "seq": 1, "index": 1, "fast": True, "scrub": True}], time.perf_counter()
            )
            assert widths == [(1, 640)]
            assert loop.refine_due is None
            loop._answer([{"op": "frame", "seq": 2, "index": 2, "fast": True}], time.perf_counter())
            assert widths[-1] == (2, 1280)
            assert conn.replies[-1][0]["index"] == 2
            assert loop.refine_due is not None
        finally:
            loop.close()


def test_warming_never_holds_the_request_thread_and_yields_on_input(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(12)])
    entered, release = threading.Event(), threading.Event()
    original = PreviewSession.fast

    def fast(self: PreviewSession, index: int, width: int = 1280, **kwargs: bool) -> Refined:
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(5)
            self.backend.check_cancelled()
        return original(self, index, width, **kwargs)

    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False)) as session:
        loop = _Loop(cast(Connection, _Connection()), session)
        monkeypatch.setattr(PreviewSession, "fast", fast)
        try:
            loop._answer([{"op": "frame", "seq": 1, "index": 6, "fast": True}], time.perf_counter())
            started = time.monotonic()
            loop._warm()
            assert time.monotonic() - started < 0.1
            assert entered.wait(3)
            # Even an auxiliary input cancels speculation, without joining its blocked worker.
            loop._answer([{"op": "table", "seq": 2}], time.perf_counter())
            assert all(lane.cancelled.is_set() for lane in session.pipeline.lanes)
            loop._answer([{"op": "frame", "seq": 3, "index": 1, "fast": True}], time.perf_counter())
            assert loop.shown == (3, 1)
            assert session.pipeline.in_flight <= 2
        finally:
            release.set()
            loop.close()


def test_pipeline_has_two_independent_bounded_lanes_and_reuses_them(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(8)])
    entered = threading.Barrier(3)
    release = threading.Event()
    original = PreviewBackend.develop
    devices: list[int] = []

    def develop(self: PreviewBackend, dev: Developer, raw: np.ndarray, step: int, width: int) -> Image:
        devices.append(id(self))
        entered.wait(5)
        assert release.wait(5)
        return original(self, dev, raw, step, width)

    monkeypatch.setattr(PreviewBackend, "develop", develop)
    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False)) as session:
        first = session.pipeline.submit(1, 32)
        second = session.pipeline.submit(2, 32)
        try:
            entered.wait(5)
            assert session.pipeline.in_flight == 2
            assert session.pipeline.submit(3, 32) is None
            assert len(set(devices)) == 2
        finally:
            release.set()
        assert first is not None
        assert second is not None
        assert first.result(5).frame.size == second.result(5).frame.size
        monkeypatch.setattr(PreviewBackend, "develop", original)
        later = session.pipeline.submit(4, 32)
        assert later is not None
        later.result(5)
        assert session.pipeline.in_flight == 0


def test_cancelled_pipeline_cannot_publish_into_new_recipe_caches(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False)) as session:
        task = session.pipeline.submit(1, 32)
        assert task is not None
        task.result(5)
        session.set_recipe(build_recipe([{"develop": {"exposure": 2}}]), fast=True)
        expected = session.fast(1, 32).jpeg
        assert expected != task.result().frame.jpeg
        assert session.fast(1, 32).jpeg == expected


def test_drag_jpeg_quality_is_separate_from_rest_and_cached_correctly(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cv2

    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    qualities: list[int] = []
    original = cv2.imencode

    def encode(extension: str, image: np.ndarray, params: list[int]) -> tuple[bool, np.ndarray]:
        qualities.append(params[1])
        return original(extension, image, params)

    monkeypatch.setattr(cv2, "imencode", encode)
    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False, quality=85)) as session:
        session.fast(0, drag=True)
        session.fast(0)
        session.fast(0, drag=True)
    assert qualities == [65, 85]


def test_retired_cache_releases_payloads_and_rejects_late_warm_results() -> None:
    from mcrawsome.preview.cache import FrameCache

    cache = FrameCache[bytes](8)
    cache.put(0, b"old", 3)
    cache.retire()
    cache.put(1, b"late", 4)
    assert cache.bytes == 0
    assert cache.get(0) is None
    assert cache.get(1) is None


def test_new_input_interrupts_an_obsolete_draft_between_stages(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    newest = [1]
    original = PreviewBackend.develop

    def develop(self: PreviewBackend, dev: Developer, raw: np.ndarray, step: int, width: int) -> Image:
        newest[0] = 2
        self.check_cancelled()
        return original(self, dev, raw, step, width)

    monkeypatch.setattr(PreviewBackend, "develop", develop)
    with PreviewSession(path, build_recipe([]), PreviewSettings(gpu=False)) as session:
        conn = _Connection()
        loop = _Loop(cast(Connection, conn), session, newest=lambda: newest[0])
        try:
            loop._answer([{"op": "frame", "seq": 1, "index": 0, "fast": True}], time.perf_counter())
            assert conn.replies == []
            loop._answer([{"op": "frame", "seq": 2, "index": 1, "fast": True}], time.perf_counter())
            assert conn.replies[-1][0]["seq"] == 2
        finally:
            loop.close()
