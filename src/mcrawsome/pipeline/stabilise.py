"""The motion step of an export: find the gyro log, fit the sync, plan the stabilisation.

Every decision not to stabilise is explained in a note; with `stabilise = "on"` it is an error instead.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.profile import CameraProfile, find_profile, focal_from_metadata
from mcrawsome.camera.recorded import recorded_camera
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import DirectLogInfo, camera_from_minf, read_gyro
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.benefit import shake_removed_by_plan
from mcrawsome.motion.flow import MotionTrack, measure_motion_raw, measure_motion_video
from mcrawsome.motion.optical import (
    FeatureTracks,
    OpticalTrackingError,
    refine_orientation,
    track_raw,
    track_video,
)
from mcrawsome.motion.orientation import Orientation
from mcrawsome.motion.plan import StabilisationPlan, plan_stabilisation
from mcrawsome.motion.raw_gyro import GyroError, raw_gyro
from mcrawsome.motion.sync import GyroKind, SyncResult, fit_sync, turning_window
from mcrawsome.recipe.schema import Recipe

MIN_COVERAGE = 0.9
MIN_BENEFIT = 0.15  # in auto, a plan that removes less of the frame-to-frame shake is not worth the crop
MIN_CORRECTION_DEG = 0.05


class StabiliseError(Exception):
    """Stabilisation was asked for ("on") but cannot be done."""


@dataclass(frozen=True)
class GyroFit:
    """What the sync fit found, for reports and Gyroflow export."""

    fit: SyncResult
    log: GyroLog
    profile: CameraProfile  # the profile the fit started from, with the readout it used
    first_centre_s: float  # the log time of frame 0's centre (mid-exposure, middle row), synced
    size: tuple[int, int]
    fps: float


@dataclass
class MotionOutcome:
    plan_path: Path | None
    notes: list[str] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=lambda: {"stabilised": False})
    gyro: GyroFit | None = None


_OUTCOME, _PLAN = "outcome.json", "stabilisation.npz"


def save_outcome(outcome: MotionOutcome, device: dict[str, str], folder: Path) -> None:
    """Keep a motion step's result (its plan, notes and measurements) in `folder` for load_outcome."""
    folder.mkdir(parents=True, exist_ok=True)
    if outcome.plan_path is not None:
        shutil.copyfile(outcome.plan_path, folder / _PLAN)
    document = {
        "planned": outcome.plan_path is not None,
        "notes": outcome.notes,
        "measurements": outcome.measurements,
        "device": device,
    }
    part = folder / f"{_OUTCOME}.part"
    part.write_text(json.dumps(document), encoding="utf-8")
    part.replace(folder / _OUTCOME)  # written last: a folder without it holds nothing usable


def load_outcome(folder: Path, staging: Path) -> tuple[MotionOutcome, dict[str, str]] | None:
    """A result kept by save_outcome, with its plan copied into `staging`; None when there is none."""
    try:
        document = json.loads((folder / _OUTCOME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan_path = None
    if document["planned"]:
        plan_path = staging / _PLAN
        shutil.copyfile(folder / _PLAN, plan_path)
    return MotionOutcome(plan_path, document["notes"], document["measurements"]), document["device"]


def _skip(recipe: Recipe, reason: str) -> MotionOutcome:
    if recipe.motion.stabilise == "on":
        raise StabiliseError(reason)
    return MotionOutcome(None, [f"not stabilised: {reason}"])


def focal_is_known(clip: Clip, recipe: Recipe) -> bool:
    """Whether camera_profile's focal length is a measurement (a Recipe override, the recording's own lens
    calibration, a profile or the lens's recorded 35 mm-equivalent focal length) rather than its 0.78 x
    width guess."""
    meta, first = clip.metadata, clip.frame_metadata(0)
    profile = find_profile(meta.device_model, meta.camera_id, first.width, first.height, clip.fps)
    return (
        recipe.camera.focal_px is not None
        or recorded_camera(meta, clip.frame_timing(0)) is not None
        or (profile is not None and profile.focal_px is not None)
        or bool(meta.focal_lengths)
    )


def camera_profile(clip: Clip, recipe: Recipe) -> CameraProfile:
    """The matching profile (or an empty one), with the Recipe's [camera] overrides and a focal length.

    A recording with its own lens calibration (MotionCam 5.1 and later) supplies the focal length, readout,
    gyro and gravity axes and lens geometry in place of a profile's; Recipe overrides still win."""
    first = clip.frame_metadata(0)
    meta = clip.metadata
    profile = find_profile(meta.device_model, meta.camera_id, first.width, first.height, clip.fps)
    profile = profile or CameraProfile(name="unknown")
    update: dict[str, Any] = {}
    recorded = recorded_camera(meta, clip.frame_timing(0))
    if recorded is not None:
        notes = (profile.description, "lens calibration recorded in the clip")
        update["description"] = "; ".join(note for note in notes if note)
        if recorded.readout_s is not None:
            update["readout_ms"] = recorded.readout_s * 1000
        if recorded.gyro_axes is not None:
            update["mcraw_axes"] = recorded.gyro_axes
        if recorded.gravity_axes is not None:
            update["gcsv_gravity_axes"] = recorded.gravity_axes
            update["gcsv_gravity_camera_ids"] = (meta.camera_id,) if meta.camera_id else ()
        if (lens := recorded.lens_calibration()) is not None:
            update["lens_calibration"] = lens
    focal = recipe.camera.focal_px or (recorded.focal_px if recorded is not None else profile.focal_px)
    if focal is None and meta.focal_lengths:
        focal = focal_from_metadata(meta.focal_lengths[0], first.original_width, first.original_height)
    update["focal_px"] = focal or 0.78 * first.width
    if recipe.camera.readout_ms is not None:
        update["readout_ms"] = recipe.camera.readout_ms
    if recipe.camera.sync_ms is not None:
        update["gcsv_sync_ms"] = recipe.camera.sync_ms
    return profile.model_copy(update=update)


def plan_raw_clip(
    clip_path: Path,
    recipe: Recipe,
    workers: int,
    staging: Path,
    *,
    frame_global: bool = False,
    allow_unverified_sync: bool = False,
    row_bands: int = 0,
    horizon_rotation: int = 0,
    check: Callable[[], None] = lambda: None,
    stage: Callable[[str], None] = lambda _text: None,
) -> MotionOutcome:
    """`stage` is told what the analysis is doing, for a page waiting on it.
    Use frame_global for a native timeline: readout still anchors exposure timing,
    but the generated plan explicitly has one centre rotation and no row correction.
    With `row_bands` as well, the native timeline plan keeps the readout and has that many row bands.
    allow_unverified_sync (gyro timelines) uses the fitted parameters even when the picture cannot confirm
    them, recording the reason; physical exports keep the fit's quality gate.
    """
    if allow_unverified_sync and not frame_global:
        raise StabiliseError("Unchecked gyro is supported only for frame-global timeline plans")
    check()
    if recipe.motion.stabilise == "off":
        return MotionOutcome(None)
    if recipe.develop.crop != "none":
        return _skip(recipe, "a cropped frame (develop.crop) is not stabilised yet")
    stage("Reading its gyro")
    with Clip(clip_path) as clip:
        try:
            source = raw_gyro(clip)
        except GyroError as exc:
            return _skip(recipe, str(exc))
        if source is None:
            name = clip_path.with_suffix(".gcsv").name
            return _skip(recipe, f"no gyro log ({name}) next to the clip, and no motion recorded in it")
        frames = [clip.frame_timing(i) for i in range(clip.frame_count)]
        log, times = source.log, source.frame_times_s
        fps, metadata = clip.fps, clip.metadata
        profile = camera_profile(clip, recipe)
        known = focal_is_known(clip, recipe)
        if (
            frame_global
            and recipe.motion.horizon_lock
            and recipe.motion.mode != "rolling_shutter"
            and (
                profile.gcsv_gravity_camera_ids
                and clip.metadata.camera_id not in profile.gcsv_gravity_camera_ids
            )
        ):
            raise StabiliseError("Horizon Lock has no verified accelerometer mapping for this camera module")
    kind: GyroKind = "mcraw" if source.recorded else "gcsv"
    # With OIS on, the lens moved the picture too: the recorded shifts join the gyro as the turn they equal.
    log, shifted = source.picture_log(profile)
    exposures = np.array([f.exposure_time_ns for f in frames], dtype=np.float64) / 1e9
    exposure = float(exposures[0])
    coverage = log.coverage(float(times[0]), float(times[-1]) + 1 / fps)
    if coverage < MIN_COVERAGE:
        return _skip(recipe, f"the gyro log covers only {coverage:.0%} of the clip")
    readout = (profile.readout_ms if profile.readout_ms is not None else 0.4 / fps * 1000) / 1000
    check()

    def measure(indices: list[int]) -> MotionTrack:
        track = measure_motion_raw(clip_path, indices, workers)
        check()
        return track

    stage("Measuring the picture to check the gyro")
    fit, track, synced = _synced(log, times, exposures, measure, profile, kind, readout, recipe, fps, known)
    optical = None
    optical_note = None
    if (
        recipe.motion.optical_correction
        and recipe.motion.strength > 0
        and (fit.reason is None or allow_unverified_sync)
    ):
        try:
            stage("Tracking image detail")
            optical = track_raw(clip_path, len(times), workers, check)
        except OpticalTrackingError as exc:
            optical_note = f"Image-assisted correction unavailable: {exc}; gyro correction retained"
    stage("Planning the stabilisation")
    outcome = _finish(
        fit, log, times, exposure, readout, recipe, frames[0].width, frames[0].height, fps, profile, staging,
        frames[0].orientation == 3, track, frame_global=frame_global,
        exposures=exposures,
        allow_unverified_sync=allow_unverified_sync, row_bands=row_bands, focal_known=known,
        check=check,
        horizon_rotation=horizon_rotation,
        display_turn=horizon_rotation if frame_global else recipe.develop.rotation,
        optical_tracks=optical,
        focus_distance=np.array([
            f.focus_distance if f.focus_distance is not None else np.nan for f in frames
        ]),
    )  # fmt: skip
    if outcome.measurements is not None:
        outcome.measurements["sync_frames"] = list(synced)
        outcome.measurements["gyro_source"] = source.source
        if shifted:
            outcome.measurements["lens_shift_compensated"] = True
        if metadata.timestamp_source is not None:
            outcome.measurements["timestamp_source"] = metadata.timestamp_source
        if metadata.use_accurate_timestamp is not None:
            outcome.measurements["accurate_timestamp_recorded"] = metadata.use_accurate_timestamp
    if optical_note:
        outcome.notes.append(optical_note)
    return outcome


SHORT_SYNC_S = 5.0  # a refused sync is checked again on this much of its window, where the camera turns most


def _synced(
    log: GyroLog,
    times: npt.NDArray[np.float64],
    exposure: float | npt.NDArray[np.float64],
    measure: Callable[[list[int]], MotionTrack],
    profile: CameraProfile,
    kind: GyroKind,
    readout: float,
    recipe: Recipe,
    fps: float,
    known: bool,
) -> tuple[SyncResult, MotionTrack, tuple[int, int]]:
    """Fit the gyro timing on the `motion.sync_frames` frames where the gyro says the camera turns most,
    with the fit, the motion measured there and the frames that confirmed it (first and last). When the
    picture disagrees there (walking, or subjects moving past, move the picture in ways the gyro cannot
    explain), the SHORT_SYNC_S of that window where the camera turns most are fitted on their own, from
    the same measurements; their verdict is used when it is a confident one."""
    length = min(recipe.motion.sync_frames, len(times))
    start = turning_window(log, times, length)
    track = measure(list(range(start, start + length)))
    search = recipe.motion.search_ms / 1000 if recipe.motion.search_ms else None

    def fit(motion: MotionTrack) -> SyncResult:
        return fit_sync(
            log, times, exposure, motion, profile, kind, readout, search, recipe.motion.min_confidence,
            focal_known=known,
        )  # fmt: skip

    result, frames = fit(track), (start, start + length - 1)
    short = min(length, max(2, round(SHORT_SYNC_S * fps)))
    if result.reason is not None and short < length:
        first = start + turning_window(log, times[start : start + length], short)
        keep = (track.first >= first) & (track.first < first + short - 1)
        retry = fit(MotionTrack(track.first[keep], track.dx[keep], track.dy[keep], track.roll[keep]))
        if retry.reason is None:
            checked = (
                f"the gyro timing was confirmed on frames {first} to {first + short - 1}, where the camera "
                "turns most (the rest of the sync window moves in ways the gyro cannot explain)"
            )
            result = replace(retry, note="; ".join(n for n in (retry.note, checked) if n))
            frames = (first, first + short - 1)
    return result, track, frames


def _finish(
    fit: SyncResult,
    log: GyroLog,
    times: npt.NDArray[np.float64],
    exposure: float,
    readout: float,
    recipe: Recipe,
    width: int,
    height: int,
    fps: float,
    profile: CameraProfile,
    staging: Path,
    rotate_180: bool,
    track: MotionTrack | None = None,
    *,
    frame_global: bool = False,
    exposures: npt.NDArray[np.float64] | None = None,
    allow_unverified_sync: bool = False,
    row_bands: int = 0,
    focal_known: bool = False,
    horizon_rotation: int = 0,
    display_turn: int = 0,
    check: Callable[[], None] = lambda: None,
    optical_tracks: FeatureTracks | None = None,
    focus_distance: npt.NDArray[np.float64] | None = None,
) -> MotionOutcome:
    """With `row_bands` and a known lens focal length (`focal_known`, the profile's), the row poses are
    spread over the readout scaled by lens / fitted focal length: Resolve projects every pose with the
    fitted one, so the rows then correct as much as the lens's focal length calls for, while the frame's
    own pose, its timing and the projection stay as fitted."""
    rows_native = frame_global and row_bands > 0
    row_focal = profile.focal_px if rows_native and focal_known and profile.focal_px else fit.focal_px
    measurements: dict[str, Any] = {
        "stabilised": False,
        "sync_offset_ms": round(fit.offset_s * 1000, 1),
        "sync_confidence": round(fit.confidence, 3),
        "sync_constrained": fit.constrained,
        "gyro_axes": list(fit.axes.axes),
        "gyro_signs": list(fit.axes.signs),
        "focal_px": round(fit.focal_px, 1),
        "readout_ms": round(readout * 1000, 2),
        "camera_profile": profile.name,
    }
    if rows_native:
        measurements["row_focal_px"] = round(row_focal, 1)
    used = profile.model_copy(update={"readout_ms": readout * 1000})
    first_centre = float(times[0]) + exposure / 2 + readout / 2 + fit.offset_s
    gyro = GyroFit(fit, log, used, first_centre, (width, height), fps)
    if fit.reason is not None and not allow_unverified_sync:
        outcome = _skip(recipe, fit.reason)
        outcome.measurements = measurements
        outcome.gyro = gyro
        return outcome
    mode = recipe.motion.mode
    measurements["mode"] = mode
    rows_on = row_bands > 0 if frame_global else recipe.motion.bands > 1
    if mode == "rolling_shutter" and not rows_on:
        outcome = _skip(recipe, "correcting only the rolling shutter needs Rolling shutter compensation on")
        outcome.measurements, outcome.gyro = measurements, gyro
        return outcome
    orientation = Orientation.from_gyro(log, fit.axes, pitch=recipe.motion.pitch, gains=fit.gains)
    if fit.gains != (1.0, 1.0, 1.0):
        measurements["gyro_axis_gains"] = list(fit.gains)
    centres = times + (exposures if exposures is not None else exposure) / 2 + readout / 2 + fit.offset_s
    if optical_tracks is not None:
        orientation, optical_report = refine_orientation(
            orientation,
            centres,
            readout,
            optical_tracks,
            fit.focal_px,
            width,
            fps,
            profile.lens_calibration,
            check,
            pitch=recipe.motion.pitch,
            exposures_s=exposures,
            focus_distance=focus_distance,
        )
        measurements["optical_correction"] = optical_report
    horizon_up = None
    if frame_global and recipe.motion.horizon_lock and mode != "rolling_shutter":
        from mcrawsome.motion.horizon import reference_up

        if profile.gcsv_gravity_axes is None:
            raise StabiliseError(
                "Horizon Lock needs a verified accelerometer-to-image mapping for this camera"
            )
        try:
            horizon_up = reference_up(log, orientation, profile.gcsv_gravity_axes, centres, check=check)
        except ValueError as exc:
            raise StabiliseError(str(exc)) from exc
        measurements["horizon_lock"] = True

    def plan_at(crop: float) -> StabilisationPlan:
        check()
        rows = rows_native  # a native timeline with rolling-shutter row poses
        spread = readout * row_focal / fit.focal_px if rows else 0.0 if frame_global else readout
        return plan_stabilisation(
            orientation, centres, spread,
            recipe.motion.smoothing_seconds, crop, recipe.motion.strength,
            row_bands if rows else 1 if frame_global else recipe.motion.bands, width, height, fps,
            fit.focal_px, rotate_180=rotate_180, edge_rows=rows,
            horizon_up=horizon_up, horizon_rotation=horizon_rotation, mode=mode, display_turn=display_turn,
        )  # fmt: skip

    plan = plan_at(recipe.motion.crop)
    # Whether it is worth the crop is judged for smoothing only: the other modes are asked for by name.
    deciding = (
        recipe.motion.stabilise == "auto"
        and recipe.motion.min_confidence > 0
        and horizon_up is None
        and mode == "smooth"
    )
    if track is not None:
        benefit = shake_removed_by_plan(plan, track)
        measurements["shake_removed"] = round(benefit, 3)
        if deciding and plan.mean_correction_deg < MIN_CORRECTION_DEG:
            outcome = _skip(recipe, "the camera barely moves, so stabilising would only crop the picture")
            outcome.measurements, outcome.gyro = measurements, gyro
            return outcome
        if deciding and benefit < MIN_BENEFIT:
            larger = [c for c in (1.2, 1.3, 1.4, 1.5) if c > recipe.motion.crop]
            suggested = next((c for c in larger if shake_removed_by_plan(plan_at(c), track) >= 0.3), None)
            if suggested is not None:
                measurements["suggested_crop"] = suggested
            advice = f"; try --crop {suggested}" if suggested else ""
            reason = (
                f"stabilising at crop {recipe.motion.crop} would remove only {max(benefit, 0.0):.0%} of the "
                f"shake{advice}"
            )
            outcome = _skip(recipe, reason)
            outcome.measurements, outcome.gyro = measurements, gyro
            return outcome
    check()
    path = staging / _PLAN
    plan.save(path)
    measurements |= {
        "stabilised": True,
        "crop": recipe.motion.crop,
        "mean_correction_deg": round(plan.mean_correction_deg, 3),
        "max_correction_deg": round(plan.max_correction_deg, 3),
        "reduced_smoothing_frames": plan.reduced_frames,
        "reduced_rolling_shutter_frames": plan.reduced_rolling_shutter_frames,
        "followed_frames": plan.followed_frames,
        "gyro_coverage": round(plan.coverage, 3),
    }
    notes = [fit.note] if fit.note else []
    if mode == "rolling_shutter" and frame_global and recipe.motion.horizon_lock:
        notes.append(
            "Horizon Lock was not applied: correcting only the rolling shutter keeps the framing as shot"
        )
    if optical_tracks is not None:
        report = measurements["optical_correction"]
        if report["applied"]:
            notes.append(f"Image-assisted gyro refinement verified on {report['measured_pairs']} frame pairs")
        else:
            notes.append("Images did not provide reliable residual motion; gyro correction retained")
    if allow_unverified_sync and fit.reason is not None:
        measurements["sync_check_bypassed"] = True
        measurements["sync_rejection_reason"] = fit.reason
        notes.append(
            f"the picture could not confirm the gyro timing ({fit.reason}); the fitted timing was used"
        )
    missed = round((1.0 - plan.coverage) * len(times))
    if missed:
        notes.append(f"the gyro log misses {missed} of {len(times)} frames; they keep the nearest correction")
    if plan.reduced_frames:
        if horizon_up is not None:
            measurements["horizon_limited_frames"] = plan.reduced_frames
            notes.append(f"Horizon Lock was eased in {plan.reduced_frames} frames to stay inside the crop")
        if mode == "smooth":
            notes.append(
                f"{plan.reduced_frames} frames used shorter smoothing to stay inside the crop "
                "(fast shake is still removed)"
            )
        else:
            notes.append(
                f"{plan.reduced_frames} frames could not be held inside the crop and partly follow the "
                "camera (fast shake is still removed); a larger crop holds more"
            )
    if plan.followed_frames:
        notes.append(f"{plan.followed_frames} frames had to follow the camera's movement, shake included")
    if plan.reduced_rolling_shutter_frames:
        notes.append(
            f"{plan.reduced_rolling_shutter_frames} frames moved too fast for the crop and got less "
            "rolling-shutter correction"
        )
    return MotionOutcome(path, notes, measurements, gyro)


def plan_video(
    ffmpeg: Path, info: DirectLogInfo, recipe: Recipe, workers: int, staging: Path
) -> tuple[MotionOutcome, dict[str, str]]:
    """The motion step for a Direct Log video, using the gyro embedded in the file."""
    if recipe.motion.stabilise == "off":
        return MotionOutcome(None), {}
    log, device = read_gyro(ffmpeg, info)
    if log is None:
        return _skip(recipe, "the video has no gyro telemetry"), device
    fps = float(info.fps)
    from mcrawsome.io.video_timing import presentation_times
    from mcrawsome.output.ffmpeg import ffprobe_for

    times = presentation_times(ffprobe_for(ffmpeg), info.path).astype(np.float64) / 1e9
    coverage = log.coverage(float(times[0]), float(times[-1]) + 1 / fps)
    if coverage < MIN_COVERAGE:
        return _skip(recipe, f"the gyro telemetry covers only {coverage:.0%} of the video"), device
    model, equivalent = camera_from_minf(device.get("MINF", ""))
    profile = find_profile(model, None, info.width, info.height, fps) or CameraProfile(name="unknown")
    profile_focal = profile.focal_px
    focal = recipe.camera.focal_px or profile.focal_px
    if focal is None:  # assume a 4:3 sensor whose full width the video covers
        focal = (
            focal_from_metadata(equivalent, info.width, info.width * 3 // 4)
            if equivalent
            else 0.78 * info.width
        )
    update: dict[str, Any] = {"focal_px": focal}
    if recipe.camera.readout_ms is not None:
        update["readout_ms"] = recipe.camera.readout_ms
    if recipe.camera.sync_ms is not None:
        update["gpmf_sync_ms"] = recipe.camera.sync_ms
    profile = profile.model_copy(update=update)
    readout = (profile.readout_ms if profile.readout_ms is not None else 0.4 / fps * 1000) / 1000
    exposure = 0.5 / fps  # Direct Log files do not record the shutter; assume 180 degrees
    # The equivalent focal length assumes a 4:3 sensor the video spans: only a profile or override is known.
    known = recipe.camera.focal_px is not None or profile_focal is not None

    def measure(indices: list[int]) -> MotionTrack:
        return measure_motion_video(ffmpeg, info.path, indices, info.width, info.height)

    fit, track, synced = _synced(log, times, exposure, measure, profile, "gpmf", readout, recipe, fps, known)
    optical = None
    optical_note = None
    if recipe.motion.optical_correction and recipe.motion.strength > 0 and fit.reason is None:
        try:
            optical = track_video(ffmpeg, info.path, len(times), info.width, info.height, lambda: None)
        except OpticalTrackingError as exc:
            optical_note = f"Image-assisted correction unavailable: {exc}; gyro correction retained"
    outcome = _finish(
        fit,
        log,
        times,
        exposure,
        readout,
        recipe,
        info.width,
        info.height,
        fps,
        profile,
        staging,
        False,
        track,
        optical_tracks=optical,
    )
    if outcome.measurements is not None:
        outcome.measurements["sync_frames"] = list(synced)
    if optical_note:
        outcome.notes.append(optical_note)
    return outcome, device
