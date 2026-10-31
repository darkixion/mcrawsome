"""Capture warnings the recording's own data explains: what went wrong while recording, and why.

Every frame's metadata and every sound sample are read; no picture is decoded. Each check reports a
fact, and a fix where there is one; none judges the shot, and intended choices stay valid.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

from mcrawsome.analysis.diagnostics import _check

Tone = Literal["ok", "warn", "plain"]
FULL_SCALE = 32767
NEAR_CLIPPING_DB = -0.5
QUIET_DB = -45.0
SILENT_DB = -80.0
WHITE_BALANCE_MIRED = 15.0  # as the analysis report


class CaptureCheck(BaseModel):
    name: str
    value: str
    tone: Tone


class CaptureChecks(BaseModel):
    checks: list[CaptureCheck]


def _db(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-12)))


def _time(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}" if minutes else f"{rest:.1f} s"


def frame_rate(exposure_s: float, measured_fps: float) -> CaptureCheck | None:
    """A frame rate the phone lowered because the shutter filled the whole frame time: the clip runs a
    little under the next standard rate, which was most likely the one chosen."""
    from mcrawsome.output.ffmpeg import _STANDARD

    if not measured_fps > 0 or not exposure_s > 0 or exposure_s < 0.97 / measured_fps:
        return None
    if any(abs(measured_fps - float(rate)) / float(rate) < 0.002 for rate in _STANDARD):
        return None  # the rate held
    # Phones record whole-number rates, so the slowed clip was meant to run at the next one up.
    above = [
        float(rate) for rate in _STANDARD if rate.denominator == 1 and float(rate) > measured_fps * 1.002
    ]
    if not above or above[0] > measured_fps * 1.05:
        return None
    intended = above[0]
    shutter = f"1/{round(1 / exposure_s)} s" if exposure_s < 1 else f"{exposure_s:.2g} s"
    return CaptureCheck(
        name="Frame rate",
        value=(
            f"Recorded at {measured_fps:.2f} fps rather than {intended:g}: the {shutter} shutter fills the "
            f"whole frame time, so the phone slowed down. A faster shutter, such as 1/{round(2 * intended)} "
            f"s, keeps {intended:g} fps."
        ),
        tone="warn",
    )


def exposure_changes(
    iso: npt.NDArray[np.float64], exposure_s: npt.NDArray[np.float64]
) -> CaptureCheck | None:
    """Automatic exposure: ISO or shutter changing during the clip changes the RAW values."""
    finite = np.isfinite(iso) & np.isfinite(exposure_s) & (iso > 0) & (exposure_s > 0)
    if finite.sum() < 2:
        return None
    level = np.log2(iso[finite] * exposure_s[finite])
    changes = int((np.abs(np.diff(level)) > 0.05).sum())
    if not changes:
        return None
    span = float(level.max() - level.min())
    return CaptureCheck(
        name="Exposure",
        value=(
            f"Changes {changes} time{'s' if changes != 1 else ''} during the clip (over {span:.1f} stops): "
            "the phone was on automatic exposure. Normalise exposure in Resolve Settings evens out the steps."
        ),
        tone="warn",
    )


def white_balance_changes(kelvin: npt.NDArray[np.float64]) -> CaptureCheck | None:
    """Automatic white balance: only a label on RAW frames, which mounts take from the first frame."""
    finite = kelvin[np.isfinite(kelvin) & (kelvin > 0)]
    if len(finite) < 2:
        return None
    mireds = 1e6 / finite
    spread = float(mireds.max() - mireds.min())
    if spread <= WHITE_BALANCE_MIRED:
        return None
    return CaptureCheck(
        name="White balance",
        value=(
            f"The phone's automatic white balance moved between {finite.min():.0f} K and "
            f"{finite.max():.0f} K. RAW pixels are unaffected; Resolve receives the first frame's "
            f"{finite[0]:.0f} K for the whole clip."
        ),
        tone="plain",
    )


def focus_hunting(dioptres: npt.NDArray[np.float64], rate: float) -> CaptureCheck | None:
    """Focus reversing direction often, found as the Frequent focus changes review marker finds it."""
    from mcrawsome.analysis.review_markers import focus

    stretches = focus(np.asarray(dioptres, dtype=np.float64), rate)
    if not stretches:
        return None
    seconds = sum(marker.duration for marker in stretches) / rate
    return CaptureCheck(
        name="Focus",
        value=(
            f"Focus reverses direction often for {seconds:.1f} s (from {_time(stretches[0].frame / rate)}): "
            "hunting, or deliberate pulls. Locking focus avoids hunting."
        ),
        tone="warn",
    )


def sound(samples: npt.NDArray[np.int16], sample_rate: int) -> list[CaptureCheck]:
    """Clipped or nearly clipped peaks, a very quiet recording, and a silent or duplicated channel."""
    if samples.size == 0 or sample_rate <= 0:
        return []
    checks: list[CaptureCheck] = []
    values = samples.astype(np.int32)
    full = np.abs(values) >= FULL_SCALE
    # Three full-scale samples in a row is clipping rather than a single loud peak.
    runs = full[:-2] & full[1:-1] & full[2:] if len(full) > 2 else np.zeros((0, values.shape[1]), bool)
    clipped = np.flatnonzero(runs.any(axis=1))
    peak = int(np.abs(values).max())
    peak_db = _db(peak / 32768)
    if len(clipped):
        starts = int(1 + np.count_nonzero(np.diff(clipped) > sample_rate // 10))
        checks.append(
            CaptureCheck(
                name="Sound",
                value=(
                    f"Clipped {starts} time{'s' if starts != 1 else ''} (first at "
                    f"{_time(clipped[0] / sample_rate)}): too loud for the microphone or its level."
                ),
                tone="warn",
            )
        )
    elif peak_db >= NEAR_CLIPPING_DB:
        at = int(np.flatnonzero(np.abs(values).max(axis=1) == peak)[0])
        checks.append(
            CaptureCheck(
                name="Sound",
                value=f"Peaks within {abs(peak_db):.1f} dB of clipping (at {_time(at / sample_rate)})",
                tone="plain",
            )
        )
    rms = np.sqrt(np.mean((values / 32768.0) ** 2, axis=0))
    loudest = _db(float(rms.max()))
    if loudest < QUIET_DB:
        checks.append(
            CaptureCheck(
                name="Sound level",
                value=f"Very quiet (average {loudest:.0f} dBFS): a quiet scene, or a covered microphone",
                tone="plain",
            )
        )
    if values.shape[1] == 2:
        names = ("left", "right")
        levels = [_db(float(value)) for value in rms]
        checks.extend(
            CaptureCheck(name="Sound channels", value=f"The {names[channel]} channel is silent", tone="warn")
            for channel in range(2)
            if levels[channel] < SILENT_DB and levels[1 - channel] > QUIET_DB
        )
        if np.array_equal(values[:, 0], values[:, 1]):
            checks.append(
                CaptureCheck(
                    name="Sound channels", value="Both channels are identical (mono sound)", tone="plain"
                )
            )
    return checks


def capture_checks(path: Path, stop: threading.Event) -> CaptureChecks:
    """Every check that applies to the clip at `path`."""
    from mcrawsome.io.directlog import is_video_input

    if is_video_input(path):
        return CaptureChecks(checks=_video_checks(path, stop))
    from dataclasses import replace

    from mcrawsome.colour.white_balance import colour_info
    from mcrawsome.io.clip import Clip
    from mcrawsome.resolve.bin_white_balance import white_balance

    with Clip(path, metadata_only=True) as clip:
        count = clip.frame_count
        iso = np.full(count, np.nan)
        exposure = np.full(count, np.nan)
        focus = np.full(count, np.nan)
        neutrals: list[tuple[float, float, float]] = []
        for index in range(count):
            if index % 64 == 0:
                _check(stop)
            metadata = clip.frame_metadata(index)
            iso[index] = metadata.iso
            exposure[index] = metadata.exposure_time_ns / 1e9
            if metadata.focus_distance is not None:
                focus[index] = metadata.focus_distance
            neutrals.append(metadata.as_shot_neutral)
        measured = clip.fps
        # The Kelvin Resolve receives (as the bin's white balance), once per distinct recorded neutral.
        info = colour_info(clip.metadata, clip.frame_metadata(0))
        kelvin_of = {}
        for neutral in dict.fromkeys(neutrals):
            try:
                kelvin_of[neutral] = white_balance(replace(info, as_shot_neutral=neutral))[0]
            except ValueError:
                kelvin_of[neutral] = float("nan")
        _check(stop)
        track = clip.audio()
    checks: list[CaptureCheck | None] = [
        frame_rate(float(np.nanmedian(exposure)), measured) if count > 1 else None,
        exposure_changes(iso, exposure),
        white_balance_changes(np.array([kelvin_of[neutral] for neutral in neutrals], dtype=np.float64)),
        focus_hunting(focus, measured) if np.isfinite(focus).any() else None,
    ]
    found = [check for check in checks if check is not None]
    if track is not None:
        found += sound(track.samples, track.sample_rate)
    return CaptureChecks(checks=found)


def _video_checks(path: Path, stop: threading.Event) -> list[CaptureCheck]:
    """Direct Log videos record no per-frame exposure, white balance or focus: only the sound."""
    from mcrawsome.output.export import read_source
    from mcrawsome.recipe.schema import Recipe

    _check(stop)
    facts, _ = read_source(path, Recipe())
    _check(stop)
    if facts.track is None:
        return []
    return sound(facts.track.samples, facts.track.sample_rate)
