"""On-demand frame timing and all recorded capture metadata; no pixel decoding."""

from __future__ import annotations

import csv
import io
import math
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, Field

from mcrawsome.analysis.diagnostics import TimingDiagnostics, _check, timing_report
from mcrawsome.io.clip import Clip

MAX_METADATA_FRAMES = 256
BLUR_HELP = (
    "Shake swept during each exposure times the focal length, from the gyro log: the intended path (its "
    "0.5 s average, as a pan) is left out. Approximate; stabilisation cannot remove this blur."
)


class CapturePoint(BaseModel):
    frame: int  # zero-based recorded frame index; never an output/CFR frame
    value: float | None
    time_s: float | None = None
    first_frame: int | None = None
    last_frame: int | None = None
    minimum: float | None = None
    maximum: float | None = None


class CaptureSeries(BaseModel):
    key: str
    label: str
    unit: str
    help: str
    points: list[CapturePoint]
    group: str = "capture"
    samples: int = 0
    minimum: float | None = None
    maximum: float | None = None


class CaptureAnalytics(BaseModel):
    frames: int
    sampled_frames: int
    unreadable_frames: int
    timing: TimingDiagnostics | None
    timing_error: str | None = None
    series: list[CaptureSeries]
    shading_frames: list[int] = Field(default_factory=list)
    shading_step: int = 30
    notes: list[str] = Field(default_factory=list)


def sample_frames(count: int) -> list[int]:
    """Even spacing includes both ends, without allocating an array the size of the clip."""
    if count <= MAX_METADATA_FRAMES:
        return list(range(count))
    return [i * (count - 1) // (MAX_METADATA_FRAMES - 1) for i in range(MAX_METADATA_FRAMES)]


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None


def _black(value: Any) -> float | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    levels = [_number(item) for item in value]
    return (
        None if any(item is None for item in levels) else sum(item / 4 for item in levels if item is not None)
    )


CHANNELS = ("R", "Gr", "Gb", "B")


class ShadingMap(BaseModel):
    frame: int
    width: int
    height: int
    channels: list[list[float]]  # R, Gr, Gb, B, row-major in the recorded sensor orientation


def _shading_array(metadata: dict[str, Any]) -> npt.NDArray[np.float64] | None:
    width, height = metadata.get("lensShadingMapWidth"), metadata.get("lensShadingMapHeight")
    if (
        not isinstance(width, int)
        or not isinstance(height, int)
        or isinstance(width, bool)
        or isinstance(height, bool)
        or not 1 <= width <= 128
        or not 1 <= height <= 128
    ):
        return None
    raw = metadata.get("lensShadingMap")
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    if any(not isinstance(plane, list) or len(plane) != width * height for plane in raw):
        return None
    try:
        values = np.asarray(raw, dtype=np.float64)
    except (ValueError, TypeError, OverflowError):
        return None
    if not np.isfinite(values).all() or np.any(values <= 0):
        return None
    return values


def shading_map(metadata: dict[str, Any], frame: int) -> ShadingMap | None:
    values = _shading_array(metadata)
    if values is None:
        return None
    return ShadingMap(
        frame=frame,
        width=metadata["lensShadingMapWidth"],
        height=metadata["lensShadingMapHeight"],
        channels=values.tolist(),
    )


def measure_shading(path: Path, frame: int, stop: threading.Event) -> ShadingMap:
    _check(stop)
    with Clip(path, metadata_only=True) as clip:
        if not 0 <= frame < clip.frame_count:
            raise ValueError("Shading frame is outside the recording")
        result = shading_map(clip.raw_frame_metadata(frame), frame)
        _check(stop)
        if result is None:
            raise ValueError("No valid four-channel shading map was recorded for this frame")
        return result


def definitions() -> list[CaptureSeries]:
    fields = [
        ("iso", "ISO", "ISO", "Recorded sensor sensitivity; not a development override.", "exposure"),
        ("shutter", "Exposure", "ms", "Recorded exposure duration, converted from nanoseconds.", "exposure"),
        ("focus", "Focus distance", "dioptres", "Recorded inverse metres; zero means infinity.", "focus"),
        (
            "black",
            "Mean black level",
            "RAW code",
            "Mean of the four recorded Bayer-site black levels.",
            "levels",
        ),
        ("white", "White level", "RAW code", "Recorded clipping level; not a recipe override.", "levels"),
        ("blur", "Shake blur", "px", BLUR_HELP, "blur"),
    ]
    for i, channel in enumerate(("R", "G", "B")):
        fields.append(
            (
                f"neutral{i}",
                f"{channel} neutral",
                "coefficient",
                "Actual asShotNeutral coefficient; not reciprocal white-balance gain or Kelvin.",
                "wb",
            )
        )
    for i, channel in enumerate(CHANNELS):
        fields.extend(
            [
                (
                    f"shading{i}",
                    f"{channel} mean gain",
                    "gain",
                    "Spatial mean of the recorded sensor shading map, before crop or development overrides.",
                    "shading",
                ),
                (
                    f"black{i}",
                    f"Site {i} black",
                    "RAW code",
                    "Recorded 2x2 Bayer-site black level in row-major order; sites 0/1 are the top row.",
                    "levels",
                ),
                (
                    f"noise_scale{i}",
                    f"{channel} scale",
                    "coefficient",
                    "Recorded shot-noise coefficient S in variance = S * normalised signal + O.",
                    "noise_scale",
                ),
                (
                    f"noise_offset{i}",
                    f"{channel} offset",
                    "coefficient",
                    "Recorded read-noise coefficient O in variance = S * normalised signal + O.",
                    "noise_offset",
                ),
            ]
        )
    return [
        CaptureSeries(key=key, label=label, unit=unit, help=help_, group=group, points=[])
        for key, label, unit, help_, group in fields
    ]


def capture_values(metadata: dict[str, Any]) -> dict[str, float | None]:
    shutter = _number(metadata.get("exposureTime"), positive=True)
    result = {
        "iso": _number(metadata.get("iso"), positive=True),
        "shutter": shutter / 1e6 if shutter is not None else None,
        "focus": _number(metadata.get("focusDistance")),
        "black": _black(metadata.get("dynamicBlackLevel")),
        "white": _number(metadata.get("dynamicWhiteLevel"), positive=True),
    }
    for field, key, count in [("asShotNeutral", "neutral", 3), ("dynamicBlackLevel", "black", 4)]:
        raw = metadata.get(field)
        if isinstance(raw, list) and len(raw) == count:
            result.update(
                {f"{key}{i}": _number(value, positive=key == "neutral") for i, value in enumerate(raw)}
            )
    noise = metadata.get("noiseProfile")
    # Android's four colour channels are R, green-even, green-odd, B. Do not
    # reinterpret a DNG-style three-channel profile as four Bayer channels.
    if isinstance(noise, list) and len(noise) == 8:
        for i in range(4):
            result[f"noise_scale{i}"] = _number(noise[2 * i])
            result[f"noise_offset{i}"] = _number(noise[2 * i + 1])
    shading = _shading_array(metadata)
    if shading is not None:
        result.update({f"shading{i}": float(value) for i, value in enumerate(shading.mean(axis=1))})
    return result


def shake_blur(path: Path, stop: threading.Event | None = None) -> npt.NDArray[np.float64] | None:
    """Each recorded frame's shake blur in pixels, or None without a usable gyro log."""
    from mcrawsome.analysis.motion_insights import load

    motion = load(path, check=(lambda: _check(stop)) if stop is not None else lambda: None)
    return None if motion is None else motion.blur()


class _Graphs:
    """Aggregate every record without retaining a Python point object per frame."""

    def __init__(self, count: int) -> None:
        self.series = definitions()
        self.count = count
        self.step = max(1, math.ceil(count / 512))
        self.pending: list[list[float | None]] = [[] for _ in self.series]

    def add(self, index: int, values: dict[str, float | None]) -> None:
        for item, pending in zip(self.series, self.pending, strict=True):
            value = values.get(item.key)
            pending.append(value)
            if value is not None:
                item.samples += 1
                item.minimum = value if item.minimum is None else min(item.minimum, value)
                item.maximum = value if item.maximum is None else max(item.maximum, value)
            if (index + 1) % self.step == 0 or index == self.count - 1:
                valid = [v for v in pending if v is not None]
                item.points.append(
                    CapturePoint(
                        frame=index - (len(pending) - 1) // 2,
                        first_frame=index - len(pending) + 1,
                        last_frame=index,
                        value=sum(valid) / len(valid) if len(valid) == len(pending) else None,
                        minimum=min(valid) if valid else None,
                        maximum=max(valid) if valid else None,
                    )
                )
                pending.clear()

    def result(self) -> list[CaptureSeries]:
        return [item for item in self.series if item.samples]


def measure_capture(
    path: Path, stop: threading.Event, *, progress: Callable[[int, int], None] | None = None
) -> CaptureAnalytics:
    _check(stop)
    if path.suffix.lower() != ".mcraw":
        from mcrawsome.analysis.log_capture import measure_log_capture

        return measure_log_capture(path, stop)
    from mcrawsome.analysis.frame_values import frame_values

    with Clip(path, metadata_only=True) as clip:
        frames = clip.frame_count
        if progress:
            progress(0, frames)
        try:
            timing = timing_report(clip.timestamps_ns, stop)
            timing_error = None
        except ValueError as exc:
            timing, timing_error = None, str(exc)
        graphs = _Graphs(frames)
        step = max(30, math.ceil(max(0, frames - 1) / 255))
        candidates = set(range(0, frames, step)) | {frames - 1}
        maps = []
        unreadable = 0
        blur = shake_blur(path, stop)
        for index, row in enumerate(frame_values(clip, stop, progress)):
            if row is None:
                unreadable += 1
            values = dict(row.values) if row is not None else capture_values({})
            if blur is not None and index < len(blur) and np.isfinite(blur[index]):
                values["blur"] = float(blur[index])
            graphs.add(index, values)
            if index in candidates and row is not None and row.shading:
                maps.append(index)
        series = graphs.result()
        timestamps = clip.timestamps_ns
        for item in series:
            for point in item.points:
                point.time_s = float(timestamps[point.frame] - timestamps[0]) / 1e9
        return CaptureAnalytics(
            frames=frames,
            sampled_frames=frames,
            unreadable_frames=unreadable,
            timing=timing,
            timing_error=timing_error,
            series=series,
            shading_frames=maps,
            shading_step=step,
        )


def capture_csv(path: Path, stop: threading.Event) -> Iterator[str]:
    """Stream every measured scalar, with exact integer timestamps; never read pixels."""
    keys = [item.key for item in definitions()]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["frame", "timestamp_ns", *keys])
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    from mcrawsome.analysis.frame_values import frame_values

    blur = shake_blur(path, stop)
    with Clip(path, metadata_only=True) as clip:
        timestamps = clip.timestamps_ns
        for index, row in enumerate(frame_values(clip, stop)):
            values = dict(row.values) if row is not None else capture_values({})
            if blur is not None and index < len(blur) and np.isfinite(blur[index]):
                values["blur"] = float(blur[index])
            writer.writerow([index + 1, int(timestamps[index]), *(values.get(key) for key in keys)])
            if buffer.tell() >= 64 * 1024:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        if buffer.tell():
            yield buffer.getvalue()
