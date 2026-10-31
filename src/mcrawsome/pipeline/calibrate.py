"""Calibrating a camera from a recording: a camera profile from the widest sync fit, with the readout.

A calibration recording is a static, distant, detailed scene filmed with slow pans, tilts and rolls.
Every frame is measured, the sync and axes are fitted with the widest search and no profile, and the
rolling-shutter readout comes from the difference in sync between the top and bottom thirds.
"""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.profile import Axes, CameraProfile, Match, profile_name
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import camera_from_minf, is_video_input, probe_direct_log, read_gyro
from mcrawsome.io.gcsv import GcsvError, read_gcsv
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.flow import MotionTrack, Strips, measure_strips_raw, measure_strips_video
from mcrawsome.motion.sync import (
    MIN_CONFIDENCE,
    STRIPS,
    GyroKind,
    SyncResult,
    fit_readout,
    fit_sync,
    raw_frame_times,
)
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.pipeline.stabilise import MIN_COVERAGE

_WIDEST_SEARCH_S = 1.5


class CalibrationError(Exception):
    """The recording cannot be used for calibration."""


@dataclass(frozen=True)
class Recording:
    """What calibration needs from a RAW clip or a Direct Log video."""

    path: Path
    kind: GyroKind
    device_model: str | None
    camera_id: str | None
    width: int
    height: int
    fps: float
    log: GyroLog | None
    frame_times_s: npt.NDArray[np.float64]
    exposure_s: float | npt.NDArray[np.float64]
    measure: Callable[[Strips], list[MotionTrack]]


@dataclass(frozen=True)
class Calibration:
    profile: CameraProfile
    fit: SyncResult
    notes: list[str] = field(default_factory=list)


def _check(stop: threading.Event | None) -> None:
    if stop is not None and stop.is_set():
        raise CancelledError


def read_recording(path: Path, workers: int, *, stop: threading.Event | None = None) -> Recording:
    """The recording's camera, size, timing and gyro log (None when it has none)."""
    _check(stop)
    if is_video_input(path):
        ffmpeg, ffprobe = find_ffmpeg()
        info = probe_direct_log(ffprobe, path)
        log, device = read_gyro(ffmpeg, info)
        model, _ = camera_from_minf(device.get("MINF", ""))
        every = list(range(info.frame_count))
        fps = float(info.fps)
        return Recording(
            path, "gpmf", model, None, info.width, info.height, fps, log,
            np.arange(info.frame_count) / fps, 0.5 / fps,
            lambda strips: measure_strips_video(
                ffmpeg, path, every, info.width, info.height, strips, stop=stop
            ),
        )  # fmt: skip
    gyro_path = path.with_suffix(".gcsv")
    try:
        gcsv_log: GyroLog | None = read_gcsv(gyro_path) if gyro_path.exists() else None
    except GcsvError as exc:
        raise CalibrationError(f"the gyro log cannot be read ({exc})") from None
    with Clip(path) as clip:
        first = clip.frame_timing(0)
        receive_times = []
        exposures = []
        for i in range(clip.frame_count):
            _check(stop)
            timing = clip.frame_timing(i)
            receive_times.append(timing.received_ms)
            exposures.append(timing.exposure_time_ns / 1e9)
        received = np.array(receive_times)
        times = np.zeros(clip.frame_count)
        if gcsv_log is not None:
            try:
                times = raw_frame_times(clip.timestamps_ns, received, gcsv_log)
            except ValueError as exc:
                raise CalibrationError(str(exc)) from None
        meta, fps, count = clip.metadata, clip.fps, clip.frame_count
    every = list(range(count))
    return Recording(
        path, "gcsv", meta.device_model, meta.camera_id, first.width, first.height, fps, gcsv_log, times,
        np.array(exposures, dtype=np.float64),
        lambda strips: measure_strips_raw(path, every, workers, strips, stop=stop),
    )  # fmt: skip


def default_name(recording: Recording) -> str:
    return profile_name(recording.device_model, recording.camera_id, recording.width, recording.height)


def recording_match(recording: Recording) -> Match:
    return Match(
        device_model=recording.device_model,
        camera_id=recording.camera_id,
        width=recording.width,
        height=recording.height,
    )


def calibrate(
    recording: Recording,
    name: str | None = None,
    min_confidence: float = MIN_CONFIDENCE,
    *,
    stop: threading.Event | None = None,
) -> Calibration:
    _check(stop)
    log, kind, fps = recording.log, recording.kind, recording.fps
    if log is None:
        raise CalibrationError("the recording has no gyro log")
    times = recording.frame_times_s
    coverage = log.coverage(float(times[0]), float(times[-1]) + 1 / fps)
    if coverage < MIN_COVERAGE:
        raise CalibrationError(f"the gyro log covers only {coverage:.0%} of the recording")
    whole, top, bottom = recording.measure([(0.0, 1.0), *STRIPS])
    _check(stop)
    guess = 0.4 / fps
    fit = fit_sync(
        log, times, recording.exposure_s, whole, None, kind, guess, _WIDEST_SEARCH_S, min_confidence
    )
    _check(stop)
    axes = Axes(axes=fit.axes.axes, signs=fit.axes.signs)
    seed = CameraProfile.model_validate(
        {
            "name": "seed",
            "focal_px": fit.focal_px,
            f"{kind}_axes": axes,
            f"{kind}_sync_ms": fit.offset_s * 1000,
        }
    )
    readout, offset = fit_readout(log, times, recording.exposure_s, top, bottom, seed, kind)
    _check(stop)
    notes = []
    readout_ms: float | None = readout * 1000
    if not 0.0 <= readout * 1000 <= 100.0:
        notes.append(
            f"the readout could not be measured (the thirds gave {readout * 1000:.1f} ms); it is left out, "
            "so stabilisation assumes 0.4 frame times"
        )
        readout_ms, offset = None, fit.offset_s
    values: dict[str, Any] = {
        "name": name or default_name(recording),
        "description": (
            f"{recording.device_model or 'camera'}, {recording.width}x{recording.height}, "
            f"calibrated from {recording.path.name}"
        ),
        "match": recording_match(recording),
        "focal_px": round(fit.focal_px, 1),
        "readout_ms": None if readout_ms is None else round(readout_ms, 2),
        f"{kind}_axes": axes,
        f"{kind}_sync_ms": round(offset * 1000, 1),
        "calibrated": (
            f"{datetime.date.today().isoformat()} from {recording.path.name}: the gyro explains "
            f"{max(fit.confidence, 0.0):.0%} of the motion"
        ),
    }
    return Calibration(CameraProfile.model_validate(values), fit, notes)
