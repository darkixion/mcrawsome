"""Bounded preview charts on the preview's output-frame axis; never decode image pixels."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from mcrawsome.analysis.capture_plot import _black, _number, capture_values, sample_frames
from mcrawsome.analysis.diagnostics import _check
from mcrawsome.analysis.gyro_plot import MAX_LOG_BYTES, envelopes
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import probe_direct_log, read_gyro
from mcrawsome.io.gcsv import GcsvError, read_gcsv
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.sync import raw_frame_times
from mcrawsome.output.export import clip_sources
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.pipeline.stabilise import camera_profile
from mcrawsome.recipe.schema import Recipe


class OverlayPoint(BaseModel):
    frame: float
    value: float | None
    minimum: float | None = None
    maximum: float | None = None


class OverlaySeries(BaseModel):
    label: str
    points: list[OverlayPoint]


class OverlayChart(BaseModel):
    key: str
    label: str
    unit: str
    help: str
    series: list[OverlaySeries]


class PreviewOverlays(BaseModel):
    frames: int
    charts: list[OverlayChart]
    notes: list[str] = []


def gyro_charts(
    log: GyroLog, frame_times: np.ndarray, note: str, stop: threading.Event
) -> list[OverlayChart]:
    envelopes(log)  # reuse finite/increasing-sample validation before interpolation
    charts = []
    frames = len(frame_times)
    groups = np.array_split(np.arange(frames), min(256, frames))
    holes = log.holes()
    for key, label, unit, values in (
        ("gyro", "Gyro", "°/s", np.rad2deg(log.gyro)),
        ("acceleration", "Acceleration", "m/s²", log.accel),
    ):
        if values is None:
            continue
        series = [OverlaySeries(label=axis, points=[]) for axis in ("X", "Y", "Z")]
        previous_time = None
        for group in groups:
            _check(stop)
            middle = int(group[len(group) // 2])
            time = frame_times[middle]
            # Preserve extrema between samples of the video, not just a thinned IMU mean.
            a, b = int(group[0]), int(group[-1])
            low = float(frame_times[a] if a == 0 else (frame_times[a - 1] + frame_times[a]) / 2)
            high = float(frame_times[b] if b == frames - 1 else (frame_times[b] + frame_times[b + 1]) / 2)
            first, end = np.searchsorted(log.times_s, (low, high), side="left")
            inside = log.times_s[0] <= time <= log.times_s[-1]
            valid = inside and not np.any((holes[:, 0] < time) & (holes[:, 1] > time))
            if (
                previous_time is not None
                and series[0].points[-1].value is not None
                and np.any((holes[:, 0] < time) & (holes[:, 1] > previous_time))
            ):
                valid = False
            previous_time = time
            for axis, item in enumerate(series):
                value = float(np.interp(time, log.times_s, values[:, axis])) if valid else None
                block = values[first:end, axis]
                item.points.append(
                    OverlayPoint(
                        frame=float(middle),
                        value=value,
                        minimum=float(min(value, block.min())) if value is not None and len(block) else value,
                        maximum=float(max(value, block.max())) if value is not None and len(block) else value,
                    )
                )
        if any(point.value is not None for point in series[0].points):
            charts.append(OverlayChart(key=key, label=label, unit=unit, help=note, series=series))
    return charts


def blur_chart(
    log: GyroLog, centres: np.ndarray, exposure_s: np.ndarray | float, focal_px: float, note: str
) -> OverlayChart | None:
    """Shake blur at each displayed frame (`centres`: mid-exposure on the log's clock), grouped into at
    most 256 points with their extremes."""
    from mcrawsome.analysis.capture_plot import BLUR_HELP
    from mcrawsome.analysis.motion_summary import frame_blur, rates

    frames = len(centres)
    if frames == 0:
        return None
    try:
        blur = frame_blur(
            rates(log, float(np.min(centres)), float(np.max(centres))), centres, exposure_s, focal_px
        )
    except ValueError:
        return None
    points = []
    for group in np.array_split(np.arange(frames), min(256, frames)):
        values = blur[group]
        finite = values[np.isfinite(values)]
        middle = int(group[len(group) // 2])
        points.append(
            OverlayPoint(
                frame=float(middle),
                value=float(blur[middle]) if np.isfinite(blur[middle]) else None,
                minimum=float(finite.min()) if len(finite) else None,
                maximum=float(finite.max()) if len(finite) else None,
            )
        )
    if not any(point.value is not None for point in points):
        return None
    series = [OverlaySeries(label="Shake blur", points=points)]
    return OverlayChart(key="blur", label="Shake blur", unit="px", help=f"{BLUR_HELP} {note}", series=series)


def interval_chart(times: np.ndarray, sources: np.ndarray) -> OverlayChart | None:
    intervals = np.concatenate(([np.nan], np.diff(times).astype(float) / 1e6))[sources]
    points = []
    for group in np.array_split(np.arange(len(sources)), min(256, len(sources))):
        block = intervals[group]
        valid = block[np.isfinite(block)]
        points.append(
            OverlayPoint(
                frame=float(group[len(group) // 2]),
                value=float(valid.mean()) if len(valid) else None,
                minimum=float(valid.min()) if len(valid) else None,
                maximum=float(valid.max()) if len(valid) else None,
            )
        )
    if not any(point.value is not None for point in points):
        return None
    return OverlayChart(
        key="timing",
        label="Frame intervals",
        unit="ms",
        help="Recorded interval before the source frame; repeats share that value. "
        "All intervals contribute; whiskers preserve minima/maxima.",
        series=[OverlaySeries(label="Interval", points=points)],
    )


def measure_overlays(path: Path, recipe: Recipe, stop: threading.Event) -> PreviewOverlays:
    _check(stop)
    if path.suffix.lower() != ".mcraw":
        ffmpeg, ffprobe = find_ffmpeg()
        info = probe_direct_log(ffprobe, path)
        from mcrawsome.io.video_timing import presentation_times
        from mcrawsome.output.export import video_sources

        stamps = presentation_times(ffprobe, path)
        mapped, _ = video_sources(ffprobe, info, recipe.output)
        sources = np.asarray(mapped if mapped is not None else range(info.frame_count))
        report = PreviewOverlays(frames=len(sources), charts=[])
        timing = interval_chart(stamps, sources)
        if timing:
            timing.help = "Movie presentation timestamps; every interval contributes. " + timing.help
            report.charts.append(timing)
        log, device = read_gyro(ffmpeg, info)
        if log is not None and log.clock == "video":
            times = stamps[sources].astype(np.float64) / 1e9 + (recipe.camera.sync_ms or 0) / 1000
            report.charts.extend(
                gyro_charts(
                    log,
                    times,
                    "Recorded device axes on the video telemetry clock; saved sync offset applied.",
                    stop,
                )
            )
            from mcrawsome.analysis.motion_insights import LOG_EXPOSURE_NOTE, video_motion

            motion = video_motion(log, device, info, None)
            fps = float(info.fps)
            chart = blur_chart(log, times + 0.25 / fps, 0.5 / fps, motion.focal_px, LOG_EXPOSURE_NOTE + ".")
            if chart:
                report.charts.append(chart)
        else:
            report.notes.append("No video-clock gyro telemetry is available for this recording")
        return report
    with Clip(path) as clip:
        output = recipe.output
        sources = np.asarray(
            clip_sources(clip, output.fill_dropped, output.frame_rate, output.frame_rate_drift_ms)
        )
        frames = len(sources)
        sampled = sample_frames(frames)
        wanted = {int(sources[i]) for i in sampled}
        metadata = {}
        received, exposures = [], []
        recorded = clip.has_recorded_motion  # on the frames' own clock: no receive times needed
        has_gyro = path.with_suffix(".gcsv").is_file() and not recorded
        for index in range(clip.frame_count) if has_gyro else sorted(wanted):
            _check(stop)
            item = clip.raw_frame_metadata(index)
            if index in wanted:
                metadata[index] = item
            if has_gyro:
                received.append(item.get("recvdTimestampMs"))
                exposures.append(_number(item.get("exposureTime")) or 0)
        charts = []
        fields = [
            ("iso", "ISO", "ISO"),
            ("shutter", "Shutter duration", "ms"),
            ("focus", "Focus distance", "dioptres"),
            ("black", "Mean black level", "RAW code"),
            ("white", "White level", "RAW code"),
        ]
        points: dict[str, list[OverlayPoint]] = {key: [] for key, _, _ in fields}
        for index in sampled:
            item = metadata[int(sources[index])]
            shutter = _number(item.get("exposureTime"), positive=True)
            values = [
                _number(item.get("iso"), positive=True),
                shutter / 1e6 if shutter else None,
                _number(item.get("focusDistance")),
                _black(item.get("dynamicBlackLevel")),
                _number(item.get("dynamicWhiteLevel"), positive=True),
            ]
            for (key, _, _), value in zip(fields, values, strict=True):
                points[key].append(OverlayPoint(frame=index, value=value))
        for key, label, unit in fields:
            if any(point.value is not None for point in points[key]):
                help_ = "Recorded metadata, at most 256 sampled preview frames; brief changes may be missed."
                if key == "focus":
                    help_ += " Dioptres are inverse metres; zero means infinity."
                charts.append(
                    OverlayChart(
                        key=key,
                        label=label,
                        unit=unit,
                        help=help_,
                        series=[OverlaySeries(label=label, points=points[key])],
                    )
                )
        recorded_values = {index: capture_values(item) for index, item in metadata.items()}
        for key, label, unit, prefix, names in (
            ("wb", "White balance", "neutral", "neutral", ("R", "G", "B")),
            ("shading", "Lens shading", "mean gain", "shading", ("R", "Gr", "Gb", "B")),
            ("noise_scale", "Noise scale", "coefficient", "noise_scale", ("R", "Gr", "Gb", "B")),
            ("noise_offset", "Noise offset", "coefficient", "noise_offset", ("R", "Gr", "Gb", "B")),
        ):
            lines = [
                OverlaySeries(
                    label=name,
                    points=[
                        OverlayPoint(
                            frame=index, value=recorded_values[int(sources[index])].get(f"{prefix}{channel}")
                        )
                        for index in sampled
                    ],
                )
                for channel, name in enumerate(names)
            ]
            if any(point.value is not None for line in lines for point in line.points):
                charts.append(
                    OverlayChart(
                        key=key,
                        label=label,
                        unit=unit,
                        series=lines,
                        help="Recorded coefficients at up to 256 preview positions. RAW sensor orientation; "
                        "see Analytics and its CSV for every recorded frame.",
                    )
                )
        timing = interval_chart(clip.timestamps_ns, sources)
        if timing:
            charts.insert(0, timing)
        report = PreviewOverlays(frames=frames, charts=charts)
        if recorded:
            try:
                report.charts.extend(_recorded_gyro(clip, recipe, sources, stop))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                report.notes.append(f"Gyro overlay unavailable: {exc}")
        if has_gyro:
            try:
                log_path = path.with_suffix(".gcsv")
                before = log_path.stat()
                log = read_gcsv(log_path, max_bytes=MAX_LOG_BYTES + 1)
                after = log_path.stat()
                if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_dev,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError("gyro log changed while reading")
                if path.with_suffix(".gcsv").stat().st_size > MAX_LOG_BYTES:
                    raise ValueError("gyro overlay supports logs up to 32 MiB")
                if any(value is None for value in received):
                    raise ValueError("frame receive timestamps are missing")
                times = raw_frame_times(clip.timestamps_ns, np.asarray(received, dtype=np.float64), log)
                profile = camera_profile(clip, recipe)
                offset = profile.gcsv_sync_ms or 0
                readout = profile.readout_ms if profile.readout_ms is not None else 0.4 / clip.fps * 1000
                times = times + np.asarray(exposures) / 2e9 + readout / 2000 + offset / 1000
                note = (
                    f"Recorded device axes; receive-clock alignment and camera offset {offset:+g} ms, "
                    "mid-exposure/readout. Not visually fitted for this overlay."
                )
                gyro = gyro_charts(log, times[sources], note, stop)
                if not gyro:
                    raise ValueError("gyro timestamps do not cover the displayed video frames")
                report.charts.extend(gyro)
                focal = profile.focal_px or 0.78 * clip.frame_metadata(0).width
                exposure = np.asarray(exposures, dtype=np.float64)[sources] / 1e9
                chart = blur_chart(
                    log, times[sources], exposure, focal, "Receive-clock alignment as the gyro."
                )
                if chart:
                    report.charts.append(chart)
            except (ValueError, OSError, KeyError, TypeError, GcsvError) as exc:
                report.notes.append(f"Gyro overlay unavailable: {exc}")
        _check(stop)
        return report


def _recorded_gyro(
    clip: Clip, recipe: Recipe, sources: np.ndarray, stop: threading.Event
) -> list[OverlayChart]:
    """Gyro and shake-blur charts from the motion recorded in the clip, on its frames' own clock."""
    from mcrawsome.motion.raw_gyro import raw_gyro

    source = raw_gyro(clip)
    if source is None:
        return []
    profile = camera_profile(clip, recipe)
    exposure = np.array([clip.frame_timing(i).exposure_time_ns for i in range(clip.frame_count)]) / 1e9
    readout = profile.readout_ms if profile.readout_ms is not None else 0.4 / clip.fps * 1000
    times = source.frame_times_s + exposure / 2 + readout / 2000
    note = "Recorded device axes on the frames' own clock, mid-exposure/readout; no sync fit is needed."
    charts = gyro_charts(source.log, times[sources], note, stop)
    if not charts:
        raise ValueError("gyro timestamps do not cover the displayed video frames")
    picture, shifted = source.picture_log(profile)
    blur = blur_chart(
        picture,
        times[sources],
        exposure[sources],
        profile.focal_px or 0.78 * clip.frame_metadata(0).width,
        "On the frames' own clock" + (", OIS lens shifts included." if shifted else "."),
    )
    return [*charts, *([blur] if blur else [])]
