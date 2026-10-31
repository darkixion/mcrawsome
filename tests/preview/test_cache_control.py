"""Cache maintenance preserves in-flight work, display leases and the frame reply channel."""

import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.errors import McError
from mcrawsome.preview.display import DisplayFrames, DisplayLease, DisplayReader
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.preview.worker import PreviewWorker
from mcrawsome.recipe.load import build_recipe


def test_clear_rejects_late_encoder_writes_and_preserves_the_current_picture(tiny_raw: Path) -> None:
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    with PreviewSession(tiny_raw, recipe, PreviewSettings(gpu=False, cache_mib=1)) as session:
        late = session.draft_encoder(0, width=32)
        current = session._fast_kept
        assert session.cache_usage()["linear_bytes"] > 0
        limit = session.cache_usage()["limit_bytes"]
        session.clear_cache()
        assert session._fast_kept is current
        assert session.cache_bytes == 0
        finished = late()
        assert finished.jpeg.startswith(b"\xff\xd8")
        assert session.cache_bytes == 0, "an encoder repopulated its retired generation"
        assert session.fast(1, width=32).jpeg
        usage = session.cache_usage()
        assert 0 < usage["bytes"] <= usage["limit_bytes"] == limit == 1024**2


def test_clear_preserves_native_display_credit_and_a_running_stream(tiny_raw: Path, tmp_path: Path) -> None:
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    with (
        DisplayFrames(128 * 64, folder=tmp_path) as frames,
        PreviewSession(tiny_raw, recipe, PreviewSettings(gpu=False)) as session,
    ):
        session.fast(0, width=32)
        stream = session.pipeline.stream(0, display=frames)
        first = stream.receive(timeout=3)
        assert first is not None
        assert first[1].frame.display is not None
        draft = first[1].frame
        lease = draft.display
        assert lease is not None
        with DisplayReader(lease, folder=tmp_path) as reader:
            pixels = reader.pixels(lease)
            expected = pixels.copy()
            del pixels
            session.clear_cache()
            assert session.cache_bytes == 0
            assert session.pipeline.streaming is stream
            assert not stream.stopped.is_set()
            pixels = reader.pixels(lease)
            np.testing.assert_array_equal(pixels, expected)
            del pixels
        draft.discard()
        stream.advance(1)
        following = stream.receive(timeout=3)
        assert following is not None
        assert following[0] > first[0]
        following[1].frame.discard()


def test_worker_control_does_not_consume_or_supersede_frame_replies(tiny_raw: Path) -> None:
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    with PreviewWorker(tiny_raw, recipe, PreviewSettings(gpu=False, cache_mib=1)) as worker:
        worker.send({"seq": 6, "op": "frame", "index": 0, "fast": True, "playback": True})
        assert worker.receive(3) is not None
        worker.send({"seq": 7, "op": "frame", "index": 1, "fast": True, "playback": True})
        usage = worker.cache_usage()
        assert usage["bytes"] > 0
        assert usage["limit_bytes"] == 1024**2
        assert worker.cache_usage(clear=True)["bytes"] == 0
        reply = worker.receive(3)
        assert reply is not None
        assert reply[0]["kind"] == "draft"
        assert reply[0]["seq"] == 7
        assert reply[1].startswith(b"\xff\xd8")
        worker.send({"seq": 8, "op": "frame", "index": 2, "fast": True, "playback": True})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            reply = worker.receive(0.1)
            if reply and reply[0].get("seq") == 8:
                break
        else:
            pytest.fail("cache clear interrupted the next frame")
        assert worker.cache_usage()["bytes"] > 0
    with pytest.raises(McError, match="busy or closed"):
        worker.cache_usage(clear=True)


def test_worker_clear_preserves_a_live_stream_with_held_display_leases(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    source = make_mcraw(tmp_path / "stream.mcraw", [i * 41666667 for i in range(40)])
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    with PreviewWorker(source, recipe, PreviewSettings(gpu=False, cache_mib=1)) as worker:
        worker.send(
            {
                "seq": 1,
                "op": "frame",
                "index": 0,
                "fast": True,
                "playback": True,
                "stream": True,
                "native": True,
            }
        )
        first = worker.receive(3)
        assert first is not None
        lease = DisplayLease.model_validate(first[0]["display"])
        with DisplayReader(lease) as reader:
            pixels = reader.pixels(lease)
            expected = pixels.copy()
            del pixels
            assert worker.cache_usage(clear=True)["bytes"] == 0
            pixels = reader.pixels(lease)
            np.testing.assert_array_equal(pixels, expected)
            del pixels
        worker.send({"op": "release", "display": lease})
        worker.send({"op": "advance", "stream_seq": 1, "index": 10})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reply = worker.receive(0.1)
            if reply is None:
                continue
            header, _ = reply
            assert header["kind"] != "error", header
            if "display" in header:
                worker.send({"op": "release", "display": DisplayLease.model_validate(header["display"])})
            if header.get("index", 0) >= 10:
                assert header["stream"] is True
                assert header["seq"] == 1
                break
        else:
            pytest.fail("cache maintenance stopped the native stream")
