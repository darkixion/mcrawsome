"""Idle preparation must be useful for Play without unbounded frames or backwards LOG seeks."""

import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Never, cast

import pytest

from mcrawsome.preview.display import DisplayFrames
from mcrawsome.preview.preparation import warm_frames
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.preview.worker import Reply, _Loop
from mcrawsome.recipe.load import build_recipe


def test_log_preparation_is_forward_only_at_one_size_and_starts_where_play_will_start() -> None:
    frames = warm_frames(0, 100, 1000, 24, 640, (640, 360), 256, raw=False)
    assert [index for index, _ in frames] == list(range(100, 148))
    assert {width for _, width in frames} == {640}
    assert len(set(frames)) == len(frames)


def test_preparation_respects_cache_disable_memory_budget_and_clip_end() -> None:
    assert not warm_frames(0, 0, 1000, 24, 1280, (1280, 720), 0, raw=False)
    assert not warm_frames(0, 0, 1000, 24, 1280, (1280, 720), 1, raw=False)
    small = warm_frames(0, 0, 1000, 24, 1280, (1280, 720), 128, raw=False)
    large = warm_frames(0, 0, 1000, 24, 1280, (1280, 720), 512, raw=False)
    assert 0 < len(small) < len(large) <= 48
    assert len(large) * 1280 * 720 * 12 < 512 * 1024**2 * 3 // 4
    assert warm_frames(98, 99, 100, 24, 640, (640, 360), 256, raw=False) == [(99, 640)]


class _Connection:
    def __init__(self) -> None:
        self.replies: list[Reply] = []

    def poll(self, timeout: float) -> bool:
        return False

    def send(self, reply: Reply) -> None:
        self.replies.append(reply)


def test_idle_prepares_future_playback_without_changing_the_displayed_picture(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(80)])
    with PreviewSession(
        path, build_recipe([{"develop": {"exposure": 0}}]), PreviewSettings(gpu=False)
    ) as session:
        connection = _Connection()
        loop = _Loop(cast(Connection, connection), session)
        try:
            loop._answer(
                [{"op": "frame", "seq": 1, "index": 3, "warm_from": 25, "fast": True}],
                time.perf_counter(),
            )
            deadline = time.monotonic() + 10
            while loop.warming or session.pipeline.in_flight:
                assert time.monotonic() < deadline
                loop._warm()
                time.sleep(0.005)
            assert loop.index == 3
            assert len(connection.replies) == 1
            for index in (25, 26, 50):
                session.fast(index, 1280)
                assert session.fast_timings["cache_hit"] == 1
            assert session.cache_bytes <= session.settings.cache_mib * 1024**2

            def no_read(*args: object, **kwargs: object) -> Never:
                pytest.fail("prepared native playback read the RAW clip again")

            monkeypatch.setattr(session, "_read_raw", no_read)
            with DisplayFrames(session.full_size[0] * session.full_size[1], folder=tmp_path) as frames:
                native = session.draft_encoder(25, 1280, display=frames)()
                assert native.display is not None
                assert not native.jpeg
                native.discard()
        finally:
            loop.close()
