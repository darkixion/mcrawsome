"""Sequential LOG previews reuse decoding and preserve exact seek/repetition mapping."""

from pathlib import Path

import numpy as np
import pytest

from mcrawsome.pipeline.shared import VideoDecoder
from mcrawsome.preview import video as module
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import build_recipe


def test_video_lane_reuses_decoder_and_seeks_exactly(
    synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[VideoDecoder] = []
    original = VideoDecoder

    def decoder(*args: object, **kwargs: object) -> VideoDecoder:
        # The real reader is kept for image comparisons, including a backwards seek.
        value = original(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(value)
        return value

    monkeypatch.setattr(module, "VideoDecoder", decoder)
    with PreviewSession(synthetic_video, build_recipe([]), PreviewSettings(gpu=False)) as session:
        first = session._develop_video(0, 160).copy()
        session._develop_video(1, 160)
        third = session._develop_video(3, 160).copy()
        np.testing.assert_array_equal(session._develop_video(3, 160), third)
        assert len(opened) == 1
        np.testing.assert_array_equal(session._develop_video(0, 160), first)
        assert len(opened) == 2
        np.testing.assert_array_equal(session._develop_video(3, 160), third)
        assert first.shape == (90, 160, 3)
        assert not np.array_equal(first, third)
    assert all(decoder._proc.poll() is not None for decoder in opened)


def test_gpu_profile_or_scaling_failure_retries_the_same_picture_on_cpu(
    synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any, cast

    attempts: list[str | None] = []

    class FailedDecoder:
        def read_into(self, pixels: np.ndarray) -> bool:
            return False

        def close(self) -> None:
            pass

    def decoder(*args: Any, **kwargs: Any) -> VideoDecoder:
        attempts.append(kwargs.get("device"))
        return cast(VideoDecoder, FailedDecoder()) if kwargs.get("device") else VideoDecoder(*args, **kwargs)

    monkeypatch.setattr(module, "decode_device", lambda *args, **kwargs: "/bad/device")
    monkeypatch.setattr(module, "VideoDecoder", decoder)
    with PreviewSession(synthetic_video, build_recipe([]), PreviewSettings(gpu=False)) as session:
        actual = session._develop_video(3, 160).copy()
        np.testing.assert_array_equal(session._develop_video(3, 160), actual)
        assert attempts == ["/bad/device", None]
        assert session._video_frames is not None
        assert session._video_frames.device is None
