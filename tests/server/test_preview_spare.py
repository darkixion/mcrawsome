"""A spare preview process waits for the next clip while a window is open, and only then."""

import asyncio
import multiprocessing
import time
from collections.abc import Callable
from pathlib import Path

from mcrawsome.preview.session import PreviewSettings
from mcrawsome.recipe.load import build_recipe
from mcrawsome.server.routes.preview import PreviewHost

STEP = 41_666_667


def _until(condition: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_window_keeps_a_spare_that_the_next_preview_uses(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * STEP for i in range(6)])
    host = PreviewHost(spare_delay_s=0)
    try:
        host.window_opened(gpu=False)
        _until(lambda: host._spare is not None and host._spare.alive)
        spare = host._spare
        assert spare is not None
        worker = asyncio.run(host.open(clip, build_recipe([]), PreviewSettings(gpu=False)))
        assert worker._process.pid == spare._process.pid
        _until(lambda: host._spare is not None and host._spare is not spare)  # the next one is ready
        host.window_closed()
        assert host._spare is None
    finally:
        host.close()
    _until(lambda: multiprocessing.active_children() == [])


def test_no_spare_starts_without_a_window(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * STEP for i in range(6)])
    host = PreviewHost(spare_delay_s=0)
    try:
        asyncio.run(host.open(clip, build_recipe([]), PreviewSettings(gpu=False)))
        time.sleep(0.3)
        assert host._spare is None
        host.window_opened(gpu=False)
        host.window_closed()  # gone before its spare started
        time.sleep(0.3)
        assert host._spare is None
    finally:
        host.close()
    _until(lambda: multiprocessing.active_children() == [])
