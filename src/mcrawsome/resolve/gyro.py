"""Gyro analysis for native timelines. No native archive or RAW pixel writing here."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from mcrawsome.io.clip import Clip
from mcrawsome.io.orientation import dng_orientation
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.motion.raw_gyro import has_gyro, has_recorded_motion
from mcrawsome.mount.limits import MAX_MOUNTS
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence, source_stamp
from mcrawsome.pipeline.stabilise import GyroFit, MotionOutcome, StabiliseError, camera_profile, plan_raw_clip
from mcrawsome.recipe.clip_layer import read_layer
from mcrawsome.recipe.load import build_recipe, load_toml, lower_layer_paths
from mcrawsome.recipe.schema import Recipe
from mcrawsome.resolve.bin_archive import Media
from mcrawsome.resolve.camera_gyro import ROWS, CameraGyroCorrections, from_plan

# Prepared clips kept for later sends in this process, about 1.3 MB each: a whole timeline's worth.
PLANS_KEPT = MAX_MOUNTS
_plans: OrderedDict[str, tuple[PreparedGyro, dict[str, bytes]]] = OrderedDict()
_plans_lock = threading.Lock()
# Plans made for a timeline in this process, the preview's included (`resolve.kept_frame`), about 0.5 MB
# each: a send after the preview has planned the clip reuses its plan. Each is made once at a time.
OUTCOMES_KEPT = 8
_outcomes: OrderedDict[str, tuple[MotionOutcome, bytes]] = OrderedDict()
_planning: dict[str, tuple[threading.Lock, list[int]]] = {}  # the plans being made, and their waiters


@dataclass(frozen=True)
class PreparedGyro:
    corrections: CameraGyroCorrections
    log_path: Path  # the .gcsv, or the recording itself for motion recorded in it
    log_stamp: tuple[int, int, int, int]
    log_sha256: str  # empty for motion recorded in the clip: its stamp is checked instead
    motion: GyroFit | None = None
    plan: StabilisationPlan | None = None

    def check(self) -> None:
        if source_stamp(self.log_path) != self.log_stamp:
            raise ValueError("Gyro log changed while creating the timeline archive")


def clip_recipe(source: Path) -> Recipe:
    """The clip's saved Recipe (its folder layers and its own settings)."""
    layer = read_layer(source)
    if layer.error:
        raise ValueError(layer.error)
    return build_recipe([load_toml(p) for p in lower_layer_paths(source)] + [layer.values])


def wants_stabilisation(source: Path) -> bool:
    """Whether a timeline send stabilises this clip: its Stabilise switch is on and, unless it is set to
    always (on), it has a gyro log and a strength above zero."""
    motion = clip_recipe(source).motion
    if motion.horizon_lock and motion.stabilise != "off":
        return True  # a requested lock must report missing sensors rather than silently skip
    if motion.stabilise == "on":
        return True
    strength = motion.strength > 0 or motion.mode == "rolling_shutter"  # that mode ignores strength
    return motion.stabilise == "auto" and strength and has_gyro(source)


def timeline_recipe(requested: Recipe, name: str) -> tuple[Recipe, bool]:
    """The Recipe a stabilised timeline plans the clip `name` with, and whether it corrects the rolling
    shutter row by row. A ValueError says why the clip's settings cannot be stabilised."""
    if requested.motion.stabilise == "off":
        raise ValueError(f"Stabilise is off for {name}; switch it on in Resolve Settings")
    rolling_shutter = requested.motion.bands > 1
    if requested.motion.mode == "rolling_shutter" and not rolling_shutter:
        raise ValueError(
            f"Correcting only the rolling shutter needs Rolling shutter compensation on for "
            f"{name}; switch it on in Resolve Settings"
        )
    recipe = requested.model_copy(
        update={
            "motion": requested.motion.model_copy(
                update={"stabilise": "on", "bands": ROWS if rolling_shutter else 1}
            ),
        }
    )
    if recipe.motion.strength == 0 and recipe.motion.mode != "rolling_shutter":
        raise ValueError(f"Stabilisation strength is zero for {name}; increase it in Resolve Settings")
    return recipe, rolling_shutter


def plan_key(source: Path, recipe: Recipe, rolling_shutter: bool, display_rotation: int) -> str:
    """Everything a timeline plan of `source` depends on (`plan_raw_clip` reads no other settings), the
    user's camera profiles included."""
    log = source.with_suffix(".gcsv")
    with Clip(source) as clip:
        profile = camera_profile(clip, recipe).model_dump(mode="json")
    inputs = {
        "source": [str(source), *source_stamp(source)],
        "log": list(source_stamp(log)) if log.is_file() else None,
        "motion": recipe.motion.model_dump(mode="json"),
        "camera": recipe.camera.model_dump(mode="json"),
        "develop": [recipe.develop.rotation, recipe.develop.crop],
        "rows": rolling_shutter,
        "display_rotation": display_rotation,
        "profile": profile,
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def plan_timeline_clip(
    source: Path,
    recipe: Recipe,
    rolling_shutter: bool,
    folder: Path,
    *,
    workers: int = 2,
    display_rotation: int = 0,
    check: Callable[[], None] = lambda: None,
    stage: Callable[[str], None] = lambda _text: None,
) -> MotionOutcome:
    """Plan `source` as a stabilised timeline does, with the Recipe `timeline_recipe` gives. A plan made
    earlier for the same inputs is reused; one being made is waited for rather than made twice."""
    key = plan_key(source, recipe, rolling_shutter, display_rotation)
    with _plans_lock:
        lock, users = _planning.setdefault(key, (threading.Lock(), [0]))
        users[0] += 1
    try:
        if not lock.acquire(blocking=False):
            stage("Waiting for the same stabilisation, already being planned")
            while not lock.acquire(timeout=0.1):
                check()
        try:
            with _plans_lock:
                kept = _outcomes.get(key)
                if kept is not None:
                    _outcomes.move_to_end(key)
                    outcome, plan = copy.deepcopy(kept[0]), kept[1]
            if kept is not None and outcome.plan_path is not None:  # only planned outcomes are kept
                stage("Reusing the stabilisation planned earlier")
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / outcome.plan_path.name
                path.write_bytes(plan)
                return replace(outcome, plan_path=path)
            try:
                outcome = plan_raw_clip(
                    source,
                    recipe,
                    workers,
                    folder,
                    frame_global=True,
                    allow_unverified_sync=True,  # a gyro send always sends; physical exports keep the gate
                    row_bands=ROWS if rolling_shutter else 0,
                    horizon_rotation=display_rotation,
                    check=check,
                    stage=stage,
                )
            except StabiliseError as exc:
                raise ValueError(f"{source.name}: {exc}") from exc
            if outcome.plan_path is not None:
                with _plans_lock:
                    _outcomes[key] = copy.deepcopy(outcome), outcome.plan_path.read_bytes()
                    while len(_outcomes) > OUTCOMES_KEPT:
                        _outcomes.popitem(last=False)
            return outcome
        finally:
            lock.release()
    finally:
        with _plans_lock:
            users[0] -= 1
            if not users[0]:
                del _planning[key]


def prepare(
    mount: MountedSequence,
    media: Media,
    folder: Path,
    *,
    workers: int = 2,
    display_rotation: int = 0,
    check: Callable[[], None] = lambda: None,
    stage: Callable[[str], None] = lambda _text: None,
) -> PreparedGyro:
    """Reuse the normal camera, clock, sync, integration and smoothing pipeline.

    A stabilised timeline clip always gets a plan: when the picture cannot confirm the gyro timing, the
    fitted timing is used and the settings say so (sync_confirmed false, with the reason). The clip's
    saved crop, strength, smoothing, rolling-shutter compensation (row bands above one) and camera
    overrides are authoritative, and the effective settings are recorded too.

    A clip sent again unchanged (its recording, gyro log, settings, camera profile, mount and timeline
    rate) reuses the earlier plan instead of analysing it again.
    """
    check()
    if media.info.orientation not in (1, 3):
        raise ValueError(
            "Native gyro analysis requires recorded-orientation mounted media; "
            "send a new timeline from the Library to apply its display rotation separately"
        )
    requested = clip_recipe(mount.source)
    recipe, rolling_shutter = timeline_recipe(requested, mount.source.name)
    recorded = has_recorded_motion(mount.source)  # recorded in the clip: no log to copy or hash
    log_path = mount.source if recorded else mount.source.with_suffix(".gcsv")
    if not recorded and not log_path.is_file():
        raise ValueError(f"No MotionCam gyro log beside {mount.source.name}, and no motion recorded in it")
    log_stamp = source_stamp(log_path)
    log_bytes = b"" if recorded else log_path.read_bytes()
    digest = "" if recorded else hashlib.sha256(log_bytes).hexdigest()
    # Sequence supplies the exact dropped-frame/repetition mapping used by virtual DNG and WAV.
    with Sequence(
        mount.source,
        vignette_profile=mount.vignette_profile,
        raw_settings=mount.raw_settings,
        frame_range=mount.frame_range,
    ) as seq:
        if seq.stamp != mount.stamp or len(seq.sources) != media.frames or float(seq.rate) != media.rate:
            raise ValueError("Mounted source timing changed")
        source_frames = list(seq.sources)
        count = seq.clip.frame_count
        first_exposure_s = seq.clip.frame_timing(0).exposure_time_ns / 1e9
        for i in range(count):
            check()
            frame = seq.clip.frame_timing(i)
            orientation = dng_orientation(frame.orientation, mount.raw_settings.rotation)
            # MotionCam's screen enum is not TIFF Orientation. In particular,
            # MotionCam 2 is written as TIFF 1 by our established DNG exporter.
            if frame.orientation not in (0, 1, 2, 3):
                raise ValueError("Gyro timelines support normal and 180-degree sensor orientation only")
            if (frame.width, frame.height, orientation) != (
                media.info.width,
                media.info.height,
                media.info.orientation,
            ):
                raise ValueError("Gyro timeline source geometry changes within the recording")
        inputs = {  # everything the plan depends on, the user's camera profiles included
            "source": str(mount.source),
            "stamp": mount.stamp,
            "gyro": "recorded" if recorded else [digest, *log_stamp],
            "recipe": requested.model_dump(mode="json"),
            "raw_settings": mount.raw_settings.model_dump(mode="json"),
            "source_frames": source_frames,
            "media": [media.frames, media.rate, media.info.width, media.info.height, media.info.orientation],
            "display_rotation": display_rotation,
            "profile": camera_profile(seq.clip, recipe).model_dump(mode="json"),
        }
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    folder.mkdir(parents=True)
    kept = _kept(key)
    if kept is not None:
        stage("Reusing the stabilisation planned earlier")
        prepared, files = kept
        prepared.check()
        check()
        if not recorded:
            (folder / "source.gcsv").write_bytes(log_bytes)
        for name, data in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
        return prepared
    outcome = plan_timeline_clip(
        mount.source,
        recipe,
        rolling_shutter,
        folder,
        workers=workers,
        display_rotation=display_rotation,
        check=check,
        stage=stage,
    )
    if outcome.plan_path is None or outcome.gyro is None:
        raise ValueError("Gyro timeline analysis did not produce a synchronised plan")
    plan = StabilisationPlan.load(outcome.plan_path)
    if len(plan.rotations) != count:
        raise ValueError("Gyro plan frame count differs from the source")
    readout_ms = outcome.gyro.profile.readout_ms
    if readout_ms is None:
        raise ValueError("Gyro plan is missing exposure-centre timing")
    centre_offset_s = first_exposure_s / 2 + readout_ms / 2000
    corrections = from_plan(
        plan,
        source_frames,
        orientation=media.info.orientation,
        rotation=mount.raw_settings.rotation,
        fps=media.rate,
        centre_offset_s=centre_offset_s,
        rows=rolling_shutter,
        check=check,
    )
    corrections.validate(media.frames, media.info.width, media.info.height, media.info.orientation)
    prepared = PreparedGyro(corrections, log_path, log_stamp, digest, outcome.gyro, plan)
    prepared.check()
    if not recorded and hashlib.sha256(log_path.read_bytes()).hexdigest() != digest:
        raise ValueError("Gyro log changed during analysis")
    check()
    measured = outcome.measurements or {}
    log: dict[str, object] = {"gyro_source": "recorded in the .mcraw"}
    if recorded:
        log["lens_shift_compensated"] = bool(measured.get("lens_shift_compensated"))
    else:
        (folder / "source.gcsv").write_bytes(log_bytes)
        log = {"gcsv": str(log_path), "gcsv_sha256": digest}
    document = {
        "schema": 2,
        "sync_confirmed": not measured.get("sync_check_bypassed", False),
        "source": str(mount.source),
        "source_stamp": mount.stamp,
        **log,
        "requested_motion": requested.motion.model_dump(mode="json"),
        "effective_motion": recipe.motion.model_dump(mode="json"),
        "camera": recipe.camera.model_dump(mode="json"),
        "profile": outcome.gyro.profile.model_dump(mode="json"),
        "sync": asdict(outcome.gyro.fit),
        "first_centre_s": outcome.gyro.first_centre_s,
        "capture_fps": outcome.gyro.fps,
        "timeline_fps": media.rate,
        "source_frames": source_frames,
        "source_frame_count": count,
        "geometry": {
            "width": media.info.width,
            "height": media.info.height,
            "orientation": media.info.orientation,
        },
        "timeline_display_rotation_clockwise": display_rotation,
        "native_rotation_location": "CinemaDNG Clip Attributes, Radiometry field 19",
        "measurements": outcome.measurements,
        "notes": outcome.notes,
        "format": "Resolve 21.1.1 native Camera Gyro 0x1030001e with shared projection 0xc00000cd",
        "native_mode": "Camera Gyro",
        "camera_quaternion_order": "xyzw",
        "camera_row_slots": 12,
        "queue_timebase": "local timeline seconds plus exposure/readout centre, stored float32 nanoseconds",
        "queue_centre_offset_s": centre_offset_s,
        "native_projection": {
            "width": corrections.width,
            "height": corrections.height,
            "focal_px": corrections.focal_px,
            "zoom": corrections.zoom,
        },
        "frame_global": not rolling_shutter,
        "row_readout_for_timing_only": not rolling_shutter,
        "rolling_shutter_rows": rolling_shutter,
        "row_slot_order": (
            "slot k at stored sensor row k/11, first row first, linear between slots (probe, 2026-10-21)"
            if rolling_shutter
            else "one pose repeated"
        ),
        "projection": "centred pinhole; no lens distortion correction",
    }
    (folder / "settings.json").write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    files = {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file() and path != folder / "source.gcsv"  # the log is copied afresh each time
    }
    with _plans_lock:
        _plans[key] = copy.deepcopy(prepared), files
        while len(_plans) > PLANS_KEPT:
            _plans.popitem(last=False)
    return prepared


def _kept(key: str) -> tuple[PreparedGyro, dict[str, bytes]] | None:
    """A plan prepared earlier for these inputs: a copy, which the caller may change."""
    with _plans_lock:
        kept = _plans.get(key)
        if kept is None:
            return None
        _plans.move_to_end(key)
        return copy.deepcopy(kept[0]), kept[1]
