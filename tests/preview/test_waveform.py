"""A clip's sound as peaks across its timeline, for the trim bar: the sound the preview plays."""

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.preview import waveform as module
from mcrawsome.preview.waveform import POINTS, waveform

STEP = 41_666_667  # 24 fps


def _quiet_then_loud(tmp_path: Path, make_mcraw: Callable[..., Path]) -> Path:
    frames = 48  # two seconds
    sound = np.zeros((round((frames - 1) * STEP / 1e9 * 48000) + 4800, 2), np.int16)
    sound[len(sound) // 2 :] = 16384  # silent, then half of full scale
    return make_mcraw(tmp_path / "loud.mcraw", [i * STEP for i in range(frames)], audio=sound)


def test_a_raw_clips_peaks_follow_its_sound_across_the_timeline(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    peaks = waveform(_quiet_then_loud(tmp_path, make_mcraw))
    assert len(peaks) == POINTS
    assert max(peaks[: POINTS * 4 // 10]) == 0
    assert min(peaks[POINTS * 6 // 10 :]) == pytest.approx(0.5, abs=0.001)


def test_peaks_are_measured_once(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = _quiet_then_loud(tmp_path, make_mcraw)
    first = waveform(clip)
    monkeypatch.setattr(module, "_measure", lambda path: pytest.fail("measured again"))
    assert waveform(clip) == first


def test_a_video_clips_peaks_come_from_its_sound(synthetic_video: Path) -> None:
    peaks = waveform(synthetic_video)
    assert len(peaks) == POINTS
    assert 0.1 < min(peaks) <= max(peaks) < 0.2  # FFmpeg's test tone at an eighth of full scale, through AAC
