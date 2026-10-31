"""Bounded, cancellable checkerboard analysis of an unwarped RAW or video recording."""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
import tempfile
import threading
from collections.abc import Generator
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from mcrawsome.camera.profile import CameraProfile, Match
from mcrawsome.camera.target import MAX_VIEWS, Model, Observation, detect, diverse, solve
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import is_video_input, probe_direct_log
from mcrawsome.motion.gyroflow import lens_profile
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.pipeline.calibrate import CalibrationError, calibrate, read_recording

MAX_SAMPLES = 480
DETECT_WIDTH = 1600
MAX_PIXELS = 64 * 1024 * 1024


class LensCaptureOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_]*$")
    camera_brand: str = Field("", max_length=100)
    camera_model: str = Field("", max_length=100)
    lens_model: str = Field("", max_length=100)
    camera_setting: str = Field("", max_length=200)
    calibrated_by: str = Field("", max_length=100)
    model: Model = "opencv_standard"


class LensCaptureResult(BaseModel):
    profile: CameraProfile
    document: dict[str, Any]
    passed: bool
    issues: list[str]


class CaptureRecording(BaseModel):
    path: str
    name: str
    kind: str
    width: int
    height: int
    fps: float
    camera: str | None = None
    gyro: bool = False


def inspect_capture(path: Path) -> CaptureRecording:
    """Probe a calibration input without importing it or applying colour-profile eligibility rules."""
    path = path.expanduser().resolve()
    if not path.is_file() or (path.suffix.lower() != ".mcraw" and not is_video_input(path)):
        raise ValueError("Choose a readable .mcraw, .mp4, .mov or .mkv calibration recording")
    if is_video_input(path):
        _, ffprobe = find_ffmpeg()
        info = probe_direct_log(ffprobe, path)
        _size(info.width, info.height)
        return CaptureRecording(
            path=str(path),
            name=path.name,
            kind="video",
            width=info.width,
            height=info.height,
            fps=float(info.fps),
            gyro=info.has_telemetry,
        )
    with Clip(path, metadata_only=True) as clip:
        frame = clip.frame_metadata(0)
        _size(frame.width, frame.height)
        return CaptureRecording(
            path=str(path),
            name=path.name,
            kind="raw",
            width=frame.width,
            height=frame.height,
            fps=clip.fps,
            camera=clip.metadata.device_model,
            gyro=clip.has_recorded_motion or path.with_suffix(".gcsv").is_file(),
        )


def check(stop: threading.Event) -> None:
    if stop.is_set():
        raise CancelledError


def sample_indices(count: int, fps: float) -> list[int]:
    if count < 1 or not np.isfinite(fps) or fps <= 0:
        raise ValueError("The recording has no valid frames or frame rate")
    samples = min(count, MAX_SAMPLES, max(1, round(count / fps * 3)))
    return np.unique(np.linspace(0, count - 1, samples).astype(int)).tolist()  # type: ignore[no-any-return]


def _size(width: int, height: int) -> tuple[int, int]:
    if min(width, height) <= 0 or width * height > MAX_PIXELS:
        raise ValueError("Calibration needs positive dimensions with at most 64 Mi pixels")
    scale = min(1.0, DETECT_WIDTH / max(width, height))
    return round(width * scale), round(height * scale)


def _raw_images(
    clip: Clip, indices: list[int], stop: threading.Event
) -> Generator[tuple[int, npt.NDArray[np.uint8]]]:
    codes = {
        "rggb": cv2.COLOR_BayerBG2GRAY,
        "bggr": cv2.COLOR_BayerRG2GRAY,
        "grbg": cv2.COLOR_BayerGB2GRAY,
        "gbrg": cv2.COLOR_BayerGR2GRAY,
    }
    first = clip.frame_metadata(0)
    size = _size(first.width, first.height)
    for index in indices:
        check(stop)
        raw = clip.raw_frame(index)
        if (
            raw.metadata.width,
            raw.metadata.height,
            raw.metadata.original_width,
            raw.metadata.original_height,
            raw.metadata.sensor_left,
            raw.metadata.sensor_top,
        ) != (
            first.width,
            first.height,
            first.original_width,
            first.original_height,
            first.sensor_left,
            first.sensor_top,
        ):
            raise ValueError("The recording changes resolution or sensor crop; use one fixed camera mode")
        grey = cv2.cvtColor(raw.data, codes[clip.metadata.sensor_arrangement]).astype(np.float32)
        black = float(np.mean(raw.metadata.black_level))
        values = np.clip((grey - black) / max(raw.metadata.white_level - black, 1), 0, 1)
        image = np.asarray(np.sqrt(values) * 255, dtype=np.uint8)
        yield index, np.asarray(cv2.resize(image, size, interpolation=cv2.INTER_AREA), dtype=np.uint8)


def _video_images(
    path: Path, width: int, height: int, indices: list[int], stop: threading.Event
) -> Generator[tuple[int, npt.NDArray[np.uint8]]]:
    ffmpeg, _ = find_ffmpeg()
    small = _size(width, height)
    if not indices:
        return

    def selection(frames: list[int]) -> str:
        if len(frames) == 1:
            return f"eq(n\\,{frames[0]})"
        middle = len(frames) // 2
        # A flat sum exceeds FFmpeg's expression parser depth on longer recordings.
        # Balance the tree to keep depth logarithmic while selecting exact frame numbers.
        return f"({selection(frames[:middle])}+{selection(frames[middle:])})"

    expression = selection(indices)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [
                str(ffmpeg),
                "-nostdin",
                "-v",
                "error",
                "-noautorotate",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-vf",
                f"select={expression},scale={small[0]}:{small[1]}:flags=area,format=gray",
                "-fps_mode",
                "passthrough",
                "-f",
                "rawvideo",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        assert process.stdout is not None
        try:
            for index in indices:
                data = bytearray()
                while len(data) < small[0] * small[1]:
                    check(stop)
                    if not select.select([process.stdout], [], [], 0.2)[0]:
                        continue
                    block = os.read(process.stdout.fileno(), small[0] * small[1] - len(data))
                    if not block:
                        errors.seek(0)
                        raise ValueError(
                            "Video decoding ended early: " + errors.read(2048).decode(errors="replace")
                        )
                    data.extend(block)
                yield index, np.frombuffer(data, np.uint8).reshape(small[1], small[0])
        finally:
            process.kill()
            process.wait()
            process.stdout.close()


def analyse_lens(path: Path, options: LensCaptureOptions, stop: threading.Event) -> LensCaptureResult:
    check(stop)
    recorded: dict[str, Any] = {}
    focus_distances: list[float] = []
    exposures: list[float] = []
    iso_values: list[float] = []
    with contextlib.ExitStack() as stack:
        if is_video_input(path):
            _, ffprobe = find_ffmpeg()
            info = probe_direct_log(ffprobe, path)
            width, height, fps, count = info.width, info.height, float(info.fps), info.frame_count
            indices = sample_indices(count, fps)
            images = _video_images(path, width, height, indices, stop)
            device, camera_id, brand = options.camera_model, None, options.camera_brand
            recorded = {
                "kind": "video",
                "variable_frame_rate": info.variable_frame_rate,
                "rotation_degrees": info.rotation,
            }
        else:
            clip = stack.enter_context(Clip(path))
            first = clip.frame_metadata(0)
            width, height, fps, count = first.width, first.height, clip.fps, clip.frame_count
            indices = sample_indices(count, fps)
            images = _raw_images(clip, indices, stop)
            device = clip.metadata.device_model or options.camera_model
            camera_id, brand = clip.metadata.camera_id, clip.metadata.manufacturer or options.camera_brand
            recorded = {
                "kind": "raw",
                "sensor_dimension": {"w": first.original_width, "h": first.original_height},
                "camera_id": camera_id,
                "focus_distance_dioptres": first.focus_distance,
                "iso": first.iso,
                "exposure_time_ns": first.exposure_time_ns,
                "cfa": clip.metadata.sensor_arrangement,
                "app_version": clip.metadata.app_version,
            }
        _size(width, height)
        views: list[Observation] = []
        sampled = detected = 0
        with contextlib.closing(images):
            for index, image in images:
                check(stop)
                sampled += 1
                if not is_video_input(path):
                    frame = clip.frame_metadata(index)
                    if frame.focus_distance is not None and np.isfinite(frame.focus_distance):
                        focus_distances.append(frame.focus_distance)
                    exposures.append(frame.exposure_time_ns)
                    iso_values.append(frame.iso)
                view = detect(image, (width, height), index, index / fps)
                if view is None:
                    continue
                detected += 1
                if diverse(view, views, (width, height)):
                    views.append(view)
        if not is_video_input(path):
            clip.check_unchanged()
    check(stop)
    # Uniformly retain poses from the whole recording, including the farther pass.
    if len(views) > MAX_VIEWS:
        views = [views[i] for i in np.linspace(0, len(views) - 1, MAX_VIEWS).astype(int)]
    lens, quality = solve(views, (width, height), options.model)
    for key, values in (
        ("focus_distance_dioptres_range", focus_distances),
        ("exposure_time_ns_range", exposures),
        ("iso_range", iso_values),
    ):
        if values:
            recorded[key] = [min(values), max(values)]
    if focus_distances and np.ptp(focus_distances) > max(0.05, abs(float(np.median(focus_distances))) * 0.1):
        quality["issues"].append(
            "Focus changed during capture. Lock focus and keep the board sharp at both distances."
        )
        quality["passed"] = False
    check(stop)
    profile = CameraProfile(
        name=options.name,
        description=f"{device or 'Camera'} {options.lens_model}: screen checkerboard",
        match=Match(device_model=device or None, camera_id=camera_id, width=width, height=height, fps=fps),
        focal_px=float((lens.camera_matrix[0][0] + lens.camera_matrix[1][1]) / 2),
        calibrated=(
            f"Screen checkerboard: RMS {lens.rms_error:.3f} px; held-out {quality['held_out_rms_px']:.3f} px"
        ),
        lens_calibration=lens,
    )
    document = lens_profile(profile, (width, height), fps, options.camera_brand or brand or "", device or "")
    document.update(
        {
            "name": options.name,
            "calibrated_by": options.calibrated_by or "MCRAWsome",
            "lens_model": options.lens_model,
            "camera_setting": options.camera_setting,
            "digital_lens": None,
            "digital_lens_params": None,
            "interpolations": None,
            "focal_length": None,
            "crop_factor": None,
            "mcrawsome": {
                "schema_version": 1,
                "camera_profile": profile.model_dump(mode="json", exclude_none=True),
                "quality": quality,
                "recorded_metadata": recorded,
                "sampling": {
                    "recording_frames": count,
                    "sampled_frames": sampled,
                    "detected_boards": detected,
                    "distinct_views": len(views),
                    "max_samples": MAX_SAMPLES,
                },
                "target": {
                    "type": "checkerboard",
                    "columns": 10,
                    "rows": 7,
                    "inner_corners": [9, 6],
                    "square_size_units": 1,
                    "physical_square_size_mm": None,
                },
                "unmeasured": [
                    "rolling_shutter_readout",
                    "gyro_axes",
                    "gyro_sync",
                    "gyro_lpf",
                    "physical_focal_length",
                    "crop_factor",
                    "colour_response",
                    "vignetting",
                    "chromatic_aberration",
                    "global_shutter",
                ],
                "assumptions": {
                    "global_shutter": False,
                    "input_stretch": [1.0, 1.0],
                    "digital_lens_transform": None,
                    "screen_is_flat": True,
                    "note": "Interchange defaults and capture assumptions, not independently measured.",
                },
                "limitations": [
                    "Screen flatness, glare, moire and focus changes affect accuracy.",
                    "Low error does not independently verify screen flatness or capture settings.",
                    "Geometry is retained for interchange; MCRAWsome does not yet warp developed pixels.",
                ],
            },
        }
    )
    return LensCaptureResult(
        profile=profile, document=document, passed=quality["passed"], issues=quality["issues"]
    )


def analyse_camera(
    path: Path,
    options: LensCaptureOptions,
    stop: threading.Event,
    *,
    gyro_path: Path | None = None,
    workers: int = 1,
) -> LensCaptureResult:
    result = analyse_lens(path, options, stop)
    if gyro_path is None:
        return result
    check(stop)
    recording = read_recording(gyro_path, workers, stop=stop)
    match = result.profile.match
    if (
        (recording.width, recording.height) != (match.width, match.height)
        or match.fps is None
        or abs(recording.fps - match.fps) >= 0.05
        or (recording.device_model and match.device_model and recording.device_model != match.device_model)
        or (recording.camera_id and match.camera_id and recording.camera_id != match.camera_id)
    ):
        raise ValueError("The gyro recording must use the same camera, lens, resolution, crop and frame rate")
    check(stop)
    try:
        measured = calibrate(recording, options.name, stop=stop)
        check(stop)
        if measured.fit.reason is not None:
            raise CalibrationError(measured.fit.reason)
    except CalibrationError as exc:
        warning = f"Gyro calibration was not trusted: {exc}. Lens geometry is still available."
        result.document["mcrawsome"]["gyro_calibration"] = {"passed": False, "issues": [warning]}
        return result.model_copy(update={"issues": [*result.issues, warning]})
    # A motion fit must never replace the separately measured optical focal length or distortion.
    fields = {"readout_ms", "gcsv_axes", "gpmf_axes", "gcsv_sync_ms", "gpmf_sync_ms"}
    profile = CameraProfile.model_validate(
        result.profile.model_dump() | measured.profile.model_dump(include=fields, exclude_none=True)
    )
    document = result.document
    document["frame_readout_time"] = profile.readout_ms
    document["mcrawsome"]["camera_profile"] = profile.model_dump(mode="json", exclude_none=True)
    document["mcrawsome"]["gyro_calibration"] = {
        "passed": True,
        "confidence": measured.fit.confidence,
        "kind": recording.kind,
        "notes": measured.notes,
        "source_name": gyro_path.name,
    }
    known = {"gyro_axes", "gyro_sync"}
    if profile.readout_ms is not None:
        known.add("rolling_shutter_readout")
    document["mcrawsome"]["unmeasured"] = [
        item for item in document["mcrawsome"]["unmeasured"] if item not in known
    ]
    return result.model_copy(
        update={"profile": profile, "document": document, "issues": [*result.issues, *measured.notes]}
    )
