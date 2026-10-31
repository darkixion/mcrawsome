"""Each frame's capture values are read once per recording and match what the full JSON gives."""

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from mcrawsome.analysis.capture_plot import capture_values
from mcrawsome.analysis.frame_values import frame_values
from mcrawsome.io.clip import Clip


def test_values_match_the_full_metadata_and_are_read_once_per_recording(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(5)])
    with Clip(source, metadata_only=True) as clip:
        rows = frame_values(clip)
        assert len(rows) == 5
        for index, row in enumerate(rows):
            full = clip.raw_frame_metadata(index)  # json.loads of everything, as the graphs read it before
            assert row is not None
            assert row.values == capture_values(full)
            assert (row.received_ms, row.exposure) == (full["recvdTimestampMs"], full["exposureTime"])
            assert row.shading == ("shading0" in capture_values(full))
    read: list[int] = []
    original = Clip.frame_metadata_json

    def counted(self: Clip, index: int) -> bytes:
        read.append(index)
        return original(self, index)

    monkeypatch.setattr(Clip, "frame_metadata_json", counted)
    with Clip(source, metadata_only=True) as clip:
        assert frame_values(clip, threading.Event()) is rows  # another graph: nothing read again
    assert read == []
    os.utime(source, ns=(os.stat(source).st_atime_ns, os.stat(source).st_mtime_ns + 1_000_000))
    with Clip(source, metadata_only=True) as clip:
        assert frame_values(clip) is not rows  # a changed recording is read afresh
    assert read == list(range(5))


def test_the_gyro_graphs_timing_reads_only_what_analytics_has_not(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis.frame_values import frame_timing

    first = make_mcraw(tmp_path / "first.mcraw", [i * 41_666_667 for i in range(4)])
    second = make_mcraw(tmp_path / "second.mcraw", [i * 41_666_667 for i in range(4)])
    with Clip(first, metadata_only=True) as clip:
        expected = [clip.raw_frame_metadata(index) for index in range(4)]
        timing = frame_timing(clip)  # alone: its own light read
    assert [(row.received_ms, row.exposure) for row in timing if row is not None] == [
        (full["recvdTimestampMs"], full["exposureTime"]) for full in expected
    ]
    with Clip(second, metadata_only=True) as clip:
        frame_values(clip)  # Analytics read this one
    monkeypatch.setattr(Clip, "frame_metadata_json", lambda *a: pytest.fail("read again for the gyro graph"))
    with Clip(second, metadata_only=True) as clip:
        assert len(frame_timing(clip)) == 4


def test_a_frame_whose_metadata_cannot_be_read_is_none(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(3)])
    original = Clip.frame_metadata_json
    monkeypatch.setattr(
        Clip, "frame_metadata_json", lambda self, index: b"{not json" if index == 1 else original(self, index)
    )
    with Clip(source, metadata_only=True) as clip:
        rows = frame_values(clip)
    assert [row is None for row in rows] == [False, True, False]
