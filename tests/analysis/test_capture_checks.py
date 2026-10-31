"""Capture warnings from recorded metadata and sound, with known answers."""

import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.analysis.capture_checks import (
    capture_checks,
    exposure_changes,
    focus_hunting,
    frame_rate,
    sound,
    white_balance_changes,
)
from mcrawsome.io.clip import Clip


def test_a_shutter_filling_the_frame_time_explains_a_slowed_frame_rate() -> None:
    check = frame_rate(41.905e-3, 23.792)
    assert check is not None
    assert check.tone == "warn"
    assert check.value.startswith("Recorded at 23.79 fps rather than 24: the 1/24 s shutter")
    assert "1/48 s" in check.value
    assert frame_rate(20.8e-3, 24.002) is None  # an ordinary 180-degree shutter
    assert frame_rate(41.905e-3, 24.0) is None  # the rate held
    assert frame_rate(41.905e-3, 17.0) is None  # too far below any rate to explain


def test_automatic_exposure_and_white_balance_are_reported_only_when_they_move() -> None:
    steady = np.full(10, 21.0), np.full(10, 1 / 48)
    assert exposure_changes(*steady) is None
    iso = np.r_[np.full(5, 100.0), np.full(5, 400.0)]
    check = exposure_changes(iso, np.full(10, 1 / 48))
    assert check is not None
    assert check.value.startswith("Changes 1 time during")
    assert "2.0 stops" in check.value
    assert "Normalise exposure" in check.value
    assert white_balance_changes(np.full(10, 5200.0)) is None
    moved = white_balance_changes(np.array([5398.0, 5000.0, 4414.0, 5757.0]))
    assert moved is not None
    assert moved.tone == "plain"
    assert "between 4414 K and 5757 K" in moved.value
    assert "first frame's 5398 K" in moved.value


def test_focus_hunting_matches_the_review_marker() -> None:
    rate = 24.0
    t = np.arange(0, 6, 1 / rate)
    hunting = 2 + 0.5 * np.sign(np.sin(2 * np.pi * 1.5 * t))
    check = focus_hunting(hunting, rate)
    assert check is not None
    assert "reverses direction often" in check.value
    assert focus_hunting(np.full(len(t), 2.0), rate) is None


def stereo(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    return np.stack([left, right], -1).astype(np.int16)


def test_sound_checks_find_clipping_near_clipping_quiet_and_odd_channels() -> None:
    rate = 48000
    t = np.arange(rate * 2) / rate
    tone = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    assert sound(stereo(tone, tone // 2), rate) == []
    clipped = tone.copy()
    clipped[rate : rate + 5] = 32767
    [check] = sound(stereo(clipped, tone // 2), rate)
    assert check.value.startswith("Clipped 1 time (first at 1.0 s)")
    loud = tone.copy()
    loud[rate // 2] = 32700
    [near] = sound(stereo(loud, tone // 2), rate)
    assert near.value.startswith("Peaks within 0.0 dB of clipping (at 0.5 s)")
    quiet = (tone // 400).astype(np.int16)
    assert [item.name for item in sound(stereo(quiet, quiet // 2), rate)] == ["Sound level"]
    silent = sound(stereo(tone, np.zeros_like(tone)), rate)
    assert [item.value for item in silent] == ["The right channel is silent"]
    mono = sound(stereo(tone, tone), rate)
    assert [item.value for item in mono] == ["Both channels are identical (mono sound)"]


def test_a_raw_clip_is_checked_without_decoding_pictures(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    t = np.arange(48000 * 2) / 48000
    tone = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    tone[48000:48010] = 32767
    stamps = [i * 41_666_667 for i in range(48)]
    source = make_mcraw(tmp_path / "loud.mcraw", stamps, audio=stereo(tone, tone))
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("checks decoded pixels"))
    names = [check.name for check in capture_checks(source, threading.Event()).checks]
    assert names == ["Sound", "Sound channels"]
