"""A plain-language description of everything a clip records, for `mcrawsome info`."""

from __future__ import annotations

import dataclasses
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.colour import camera_to_ap1
from mcrawsome.camera.recorded import recorded_camera
from mcrawsome.colour.encoding import colour_labels, encoding
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import (
    ENCODING_NAMES,
    aperture_from_minf,
    camera_from_minf,
    is_video_input,
    probe_direct_log,
    read_gyro,
)
from mcrawsome.io.gcsv import GcsvError, read_gcsv
from mcrawsome.output.ffmpeg import find_ffmpeg

Section = tuple[str, list[tuple[str, str]]]


def _same(values: Sequence[float]) -> bool:
    return max(values) - min(values) <= 1e-9 + 0.005 * abs(max(values))


def span_text(values: Sequence[float], fmt: str) -> str:
    """One value, or the range and median when it changed during the clip."""
    if _same(values):
        return fmt.format(values[0])
    low, high, mid = min(values), max(values), statistics.median(values)
    return f"{fmt.format(low)} to {fmt.format(high)} (median {fmt.format(mid)})"


def shutter_text(exposures_ns: Sequence[float], fps: float) -> str:
    def parts(ns: float) -> tuple[str, str, str]:
        return f"1/{round(1e9 / ns)}", f"{ns / 1e6:.2f}", f"{round(ns / 1e9 * fps * 360)}°"

    if _same(exposures_ns):
        fraction, ms, angle = parts(exposures_ns[0])
        return f"{fraction} s ({ms} ms, {angle} shutter)"
    (f1, m1, a1), (f2, m2, a2) = parts(min(exposures_ns)), parts(max(exposures_ns))
    return f"{f1} to {f2} s ({m1} to {m2} ms, {a1} to {a2} shutter)"


def focus_text(dioptres: Sequence[float]) -> str:
    """Android reports focus in dioptres (1 / metres); 0 is infinity."""

    if _same(dioptres):
        d = dioptres[0]
        return "infinity" if d <= 0 else f"{1 / d:.2f} m ({d:.2f} dioptres)"
    near, far = 1 / max(dioptres), min(dioptres)
    span = f"{near:.2f} m to infinity" if far <= 0 else f"{near:.2f} to {1 / far:.2f} m"
    return f"{span} (changed during the clip)"


def _missing_after(timestamps_ns: npt.NDArray[np.int64], step_ns: float) -> npt.NDArray[np.int64]:
    """Frames missing after each frame but the last: a gap of more than 1.5 steps is n steps (rounded),
    so n - 1 frames are missing."""
    steps = np.diff(timestamps_ns).astype(np.float64)
    return np.where(steps > 1.5 * step_ns, np.round(steps / step_ns) - 1, 0).astype(np.int64)


def dropped_frames(timestamps_ns: npt.NDArray[np.int64]) -> int:
    """Frames missing from the recording, from gaps in the frame timestamps."""
    if len(timestamps_ns) < 3:
        return 0
    step = float(np.median(np.diff(timestamps_ns).astype(np.float64)))
    return int(_missing_after(timestamps_ns, step).sum())


def frame_map(timestamps_ns: npt.NDArray[np.int64], step_ns: float) -> list[int]:
    """For each output frame, the source frame it shows: the frame before a gap is repeated once for each
    frame missing in it, so the frames keep their recorded times."""
    missing = [*_missing_after(timestamps_ns, step_ns).tolist(), 0]
    sources: list[int] = []
    for index in range(len(timestamps_ns)):
        sources += [index] * (1 + missing[index])
    return sources


def duplicated_frames(timestamps_ns: npt.NDArray[np.int64]) -> int:
    """Frames delivered twice: a step under half the median step after the frame before."""
    if len(timestamps_ns) < 3:
        return 0
    steps = np.diff(timestamps_ns).astype(np.float64)
    return int((steps < 0.5 * float(np.median(steps))).sum())


RateChoice = Literal["keep", "integer", "drop-frame", "average", "median"] | float
_DROP_FRAME = (24, 30, 60, 120)  # rates with an NTSC (1000/1001) version


def _median_fps(timestamps_ns: npt.NDArray[np.int64]) -> float:
    if len(timestamps_ns) < 2:
        return 24.0
    return float(1e9 / np.median(np.diff(timestamps_ns)))


def _average_fps(timestamps_ns: npt.NDArray[np.int64]) -> float:
    if len(timestamps_ns) < 2 or timestamps_ns[-1] <= timestamps_ns[0]:
        return _median_fps(timestamps_ns)
    return float((len(timestamps_ns) - 1) * 1e9 / (timestamps_ns[-1] - timestamps_ns[0]))


def target_rate(choice: RateChoice, timestamps_ns: npt.NDArray[np.int64]) -> Fraction | None:
    """The constant rate a frame-rate conversion aims at (None for keep): the nearest whole rate, the
    nearest drop-frame rate (23.976, 29.97, 59.94, 119.88; a whole rate when none is near), the average
    over the clip, the median frame step (for slow motion), or a number."""
    if choice == "keep":
        return None
    if isinstance(choice, float | int):
        return Fraction(choice).limit_denominator(1001)
    median = _median_fps(timestamps_ns)
    whole = max(1, round(median))
    if choice == "integer":
        return Fraction(whole)
    if choice == "drop-frame":
        return Fraction(whole * 1000, 1001) if whole in _DROP_FRAME else Fraction(whole)
    if choice == "average":
        return Fraction(_average_fps(timestamps_ns)).limit_denominator(1000)
    return Fraction(median).limit_denominator(1000)


@dataclass(frozen=True)
class FrameTiming:
    frames: int
    median_fps: float
    average_fps: float
    dropped: int
    duplicated: int
    targets: dict[str, float]  # what each output.frame_rate choice gives


def frame_timing(timestamps_ns: npt.NDArray[np.int64]) -> FrameTiming:
    targets = {
        choice: round(float(rate), 3)
        for choice in ("integer", "drop-frame", "average", "median")
        if (rate := target_rate(choice, timestamps_ns)) is not None
    }
    return FrameTiming(
        frames=len(timestamps_ns),
        median_fps=round(_median_fps(timestamps_ns), 4),
        average_fps=round(_average_fps(timestamps_ns), 4),
        dropped=dropped_frames(timestamps_ns),
        duplicated=duplicated_frames(timestamps_ns),
        targets=targets,
    )


def rate_map(timestamps_ns: npt.NDArray[np.int64], period_ns: float, drift_ns: float) -> list[int]:
    """For each output frame at a constant `period_ns`, the recorded frame it shows. Each output frame shows
    the next recorded frame, unless that frame is ahead of the output time by more than the drift limit (the
    frame before is shown again) or behind it by more (frames are skipped). The limit is at least half a
    frame, so timestamp jitter moves nothing. The output lasts as long as the recording."""
    times = timestamps_ns.astype(np.float64) - float(timestamps_ns[0])
    count = len(times)
    step = float(np.median(np.diff(times))) if count > 1 else period_ns
    limit = max(drift_ns, 0.5 * max(period_ns, step))
    sources: list[int] = []
    shown = 0
    for index in range(max(1, round((times[-1] + step) / period_ns))):
        t = index * period_ns
        frame = 0 if index == 0 else min(shown + 1, count - 1)
        if index > 0 and times[frame] - t > limit:
            frame = shown  # the recording is ahead: show the frame again
        while (
            frame + 1 < count
            and t - times[frame] > limit
            and times[frame + 1] - t <= limit
            and times[frame + 1] - t < t - times[frame]
        ):
            frame += 1  # the recording is behind: skip a frame
        sources.append(frame)
        shown = frame
    return sources


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def describe(clip: Clip) -> list[Section]:
    from mcrawsome.colour.white_balance import as_shot_values

    meta = clip.metadata
    frames = [clip.frame_metadata(i) for i in range(clip.frame_count)]
    first = frames[0]
    fps = clip.fps

    device = meta.device_model or "unknown"
    if meta.manufacturer and meta.build_model:
        device = f"{meta.manufacturer} {meta.build_model}" + (
            f" ({meta.build_device})" if meta.build_device else ""
        )
    camera = [("Device", device), ("Camera ID", meta.camera_id or "unknown")]
    if meta.app_version:
        release = f" {meta.app_release.title()}" if meta.app_release else ""
        camera.append(("Recorded with", f"MotionCam{release} {meta.app_version}"))

    lens = [
        (
            "Focal length",
            ", ".join(f"{f:.1f} mm (35 mm equivalent)" for f in meta.focal_lengths) or "not recorded",
        ),
        ("Aperture", ", ".join(f"f/{_number(a)}" for a in meta.apertures) or "not recorded"),
    ]
    focus = [f.focus_distance for f in frames if f.focus_distance is not None]
    lens.append(("Focus distance", focus_text(focus) if focus else "not recorded"))

    bits = 12 if meta.white_level > 1023 else 10
    full = (first.width, first.height) == (first.original_width, first.original_height)
    crop = "full sensor" if full else "centred crop"
    black = _number(float(np.mean(first.black_level)))
    illuminants = " and ".join(
        {"standarda": "standard A", "d65": "D65", "d50": "D50"}.get(i, i)
        for i in (meta.colour_illuminant_1, meta.colour_illuminant_2)
        if i
    )
    calibrated = ", with per-unit calibration" if meta.calibration_matrix_1 else ""
    sensor = [
        ("Full sensor", f"{first.original_width}x{first.original_height}"),
        (
            "Recorded area",
            f"{first.width}x{first.height}, {crop}" + (", binned" if first.is_binned else ""),
        ),
        (
            "Raw",
            f"{bits}-bit {meta.sensor_arrangement.upper()}, black level {black}, "
            f"white level {_number(first.white_level)}",
        ),
        ("Mounted", f"{meta.sensor_orientation}°" if meta.sensor_orientation is not None else "unknown"),
        ("Colour data", f"matrices for {illuminants or 'two illuminants'}{calibrated}"),
        (
            "Lens shading",
            f"{first.lens_shading_map_width}x{first.lens_shading_map_height} map in every frame",
        ),
        ("Noise profile", "recorded" if first.noise_profile else "not recorded"),
    ]

    step = meta.exposure_compensation_step
    ev = [
        f.exposure_compensation * step[0] / step[1]
        for f in frames
        if f.exposure_compensation is not None and step
    ]
    kelvin = [camera_to_ap1(meta, f.as_shot_neutral)[1] for f in frames]
    balance = as_shot_values(clip)
    exposure = [
        ("Frames checked", str(len(frames))),
        ("ISO", span_text([f.iso for f in frames], "{:.0f}")),
        ("Shutter", shutter_text([f.exposure_time_ns for f in frames], fps)),
        (
            "Exposure compensation",
            (span_text(ev, "{:+.2f} EV") if any(ev) else "none") if ev else "not recorded",
        ),
        ("White balance", span_text(kelvin, "{:.0f} K")),
        (
            "As Shot (median)",
            f"{balance['temperature']:.1f} K, tint {balance['tint']:+.2f}"
            if balance["temperature"] is not None and balance["tint"] is not None
            else "unavailable",
        ),
    ]

    started = datetime.fromtimestamp(first.received_ms / 1000).astimezone()
    timing = frame_timing(clip.timestamps_ns)
    dropped = timing.dropped
    audio = clip.audio()
    recording = [
        ("Started", started.strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Length", f"{clip.frame_count} frames at {fps:.3f} fps ({clip.frame_count / fps:.2f} s)"),
        ("Frame rate", f"{timing.median_fps:.2f} fps (median), {timing.average_fps:.2f} average"),
        ("Dropped frames", "none" if dropped == 0 else f"{dropped} (gaps in the frame timestamps)"),
        (
            "Duplicated frames",
            "none" if timing.duplicated == 0 else f"{timing.duplicated} (repeated frame timestamps)",
        ),
        (
            "Frame-rate targets",
            ", ".join(f"{choice} {_number(rate)}" for choice, rate in timing.targets.items()),
        ),
        ("Orientation", "upside down (will be rotated)" if first.orientation == 3 else "normal"),
        (
            "Audio",
            f"{audio.sample_rate} Hz, {audio.samples.shape[1]} channels, {clip.audio_chunk_count} chunks"
            if audio
            else "none",
        ),
    ]
    if clip.recovery_note:
        recording.append(("Recovery", clip.recovery_note))
    if clip.remosaic_note:
        recording.append(("Sensor", clip.remosaic_note))
    gyro_path = clip.gyro_log_path()
    motion = clip.recorded_motion()
    if motion is not None:
        times = motion.gyro.times_ns
        rate = (len(times) - 1) / ((times[-1] - times[0]) / 1e9) if times[-1] > times[0] else 0.0
        parts = [f"recorded in the clip: {len(times)} samples at {rate:.0f} Hz"]
        if motion.accelerometer is not None:
            parts.append("with the accelerometer")
        if motion.ois is not None:
            parts.append(f"and {len(motion.ois.times_ns)} OIS lens shifts")
        recording.append(("Gyro", " ".join(parts)))
        calibration = recorded_camera(meta, clip.frame_timing(0))
        if calibration is not None:
            text = f"recorded in the clip: focal length {calibration.focal_px:.0f} px, optical centre "
            text += f"{calibration.centre_px[0]:.0f}, {calibration.centre_px[1]:.0f}"
            if calibration.readout_s is not None:
                text += f", rolling shutter {calibration.readout_s * 1000:.1f} ms"
            recording.append(("Lens calibration", text))
    elif gyro_path:
        try:
            log = read_gcsv(gyro_path)
            recording.append(
                ("Gyro", f"{gyro_path.name}: {len(log.times_s)} samples, {log.times_s[-1]:.2f} s")
            )
        except GcsvError as exc:
            recording.append(("Gyro", f"unreadable ({exc})"))
    else:
        recording.append(("Gyro", "no .gcsv file next to the clip, and none recorded in it"))
    if meta.gps:
        lat, lon, alt = meta.gps
        recording.append(
            ("Location", f"{lat:.6f}, {lon:.6f}" + (f", {alt:.0f} m" if alt is not None else ""))
        )
    else:
        recording.append(("Location", "not recorded"))
    if meta.num_segments and meta.num_segments > 1:
        recording.append(("Segments", str(meta.num_segments)))

    settings = [(key, str(value)) for key, value in sorted(meta.app_settings.items())]
    return [
        ("Camera", camera),
        ("Lens", lens),
        ("Sensor", sensor),
        ("Exposure", exposure),
        ("Recording", recording),
        ("MotionCam settings", settings or [("(none)", "")]),
    ]


def encoding_name(key: str) -> str:
    """A colour encoding as `mcrawsome info` names it."""
    return ENCODING_NAMES.get(key) or encoding(key).name


def describe_video(path: Path) -> list[Section]:
    """A Direct Log (or other) video: picture, colour encoding, audio and gyro telemetry."""
    ffmpeg, ffprobe = find_ffmpeg()
    info = probe_direct_log(ffprobe, path)
    log, device = read_gyro(ffmpeg, info)
    colour = colour_labels(info.encoding, info.encoding_source)
    tags = info.tags
    video = [
        (
            "Picture",
            f"{info.width}x{info.height}, {info.codec.upper()} {info.bit_depth}-bit, "
            + {"pc": "full range", "tv": "video range"}.get(info.colour_range, info.colour_range),
        ),
        ("Length", f"{info.frame_count} frames at {float(info.fps):.3f} fps ({info.duration_s:.2f} s)"),
        (
            "Frame timing",
            "variable; fixed-rate preview/export from recorded timestamps"
            if info.variable_frame_rate
            else "constant",
        ),
        ("Colour space", colour["colour_space"]),
        ("Transfer function", colour["transfer_function"]),
        ("Colour source", colour["encoding_source"]),
        (
            "Container tags",
            f"{tags['primaries']} / {tags['transfer']} / {tags['matrix']} "
            "(reported, not a recording profile)",
        ),
        ("Audio", f"{info.audio_rate} Hz, {info.audio_channels} channels" if info.audio_rate else "none"),
    ]
    if info.colour_note:
        video.append(("Colour note", info.colour_note))
    model, equivalent = camera_from_minf(device.get("MINF", ""))
    camera = [
        ("Device", model or "unknown"),
        ("Focal length", f"{equivalent:.1f} mm (35 mm equivalent)" if equivalent else "not recorded"),
    ]
    aperture = aperture_from_minf(device.get("MINF", ""))
    camera.append(("Aperture", f"f/{aperture:g}" if aperture is not None else "not recorded"))
    if log is not None:
        per_frame = len(log.times_s) / max(info.frame_count, 1)
        telemetry = [
            ("Gyro", f"{len(log.times_s)} samples, about {per_frame:.0f} per frame, {log.times_s[-1]:.2f} s"),
            ("Accelerometer", "recorded" if log.accel is not None else "not recorded"),
            ("Phone stabilisation (EIS)", "off" if device.get("EISA", "N") == "N" else "on"),
        ]
    else:
        telemetry = [("Gyro", "none in this file")]
    return [("Video", video), ("Camera", camera), ("Telemetry", telemetry)]


def info_document(
    clip: Path, frames: int, sections: list[Section], timing: FrameTiming | None = None
) -> dict[str, Any]:
    """What `mcrawsome info --json` prints; a .mcraw clip's adds its frame `timing`."""
    document = {"clip": str(clip), "frames": frames} | {title: dict(rows) for title, rows in sections}
    if timing is not None:
        document["timing"] = dataclasses.asdict(timing)
    return document


def describe_any(clip: Path) -> dict[str, Any]:
    """The `info --json` document of a .mcraw clip or a video, read from its metadata only."""
    if is_video_input(clip):
        frames = probe_direct_log(find_ffmpeg()[1], clip).frame_count
        return info_document(clip, frames, describe_video(clip))
    with Clip(clip) as c:
        return info_document(clip, c.frame_count, describe(c), frame_timing(c.timestamps_ns))
