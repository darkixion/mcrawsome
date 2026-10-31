"""Camera profiles: what is known about a camera for stabilisation, as data files.

Built-in profiles live next to this module; the user's own (for example from `mcrawsome calibrate`) live
in ~/.config/mcrawsome/cameras. A profile applies when every field in its [match] table matches the clip;
the most specific profile wins, and the user's beat the built-in ones. Device names ignore case and
surrounding whitespace. Measured lens profiles retain missing gyro defaults from other matching profiles.
Verified physical axis mappings also apply across recording sizes and frame rates for the same camera
module; pixel geometry and timing measurements do not.
"""

from __future__ import annotations

import difflib
import functools
import math
import re
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcrawsome.camera.lens import LensCalibration
from mcrawsome.paths import config_dir

_FROZEN = ConfigDict(extra="forbid", frozen=True)
_EQUIVALENT_DIAGONAL_MM = 43.27  # the diagonal of a 36x24 mm frame


class ProfileError(Exception):
    """A camera profile is unreadable, invalid or cannot be saved."""


class Axes(BaseModel):
    """Which device gyro axis (0, 1, 2 for X, Y, Z) drives image-x, image-y and roll, with signs."""

    model_config = _FROZEN
    axes: tuple[int, int, int]
    signs: tuple[int, int, int]


class Match(BaseModel):
    model_config = _FROZEN
    device_model: str | None = None
    camera_id: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class CameraProfile(BaseModel):
    model_config = _FROZEN

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    description: str = ""
    match: Match = Match()
    focal_px: float | None = Field(None, gt=0, description="Focal length in pixels at the recorded size")
    readout_ms: float | None = Field(None, ge=0, le=100, description="Rolling-shutter readout, top to bottom")
    gcsv_axes: Axes | None = None
    gcsv_gravity_axes: Axes | None = Field(
        None, description="Device acceleration mapped to RAW image x/y/z; separate from gyro motion signs"
    )
    gcsv_gravity_camera_ids: tuple[str, ...] = Field(
        (), description="Camera modules with a verified gravity mapping; empty uses the profile's match"
    )
    gpmf_axes: Axes | None = None
    mcraw_axes: Axes | None = Field(
        None, description="Gyro recorded inside the .mcraw: from the recording's lens facing and sensor angle"
    )
    gcsv_sync_ms: float | None = Field(None, ge=-2000, le=2000)
    gpmf_sync_ms: float | None = Field(None, ge=-2000, le=2000)
    calibrated: str | None = Field(None, description="When and how well this profile was calibrated")
    lens_calibration: LensCalibration | None = Field(
        None, description="Measured lens geometry for interchange; not yet applied to developed pixels"
    )


def profile_name(device_model: str | None, camera_id: str | None, width: int, height: int) -> str:
    device = re.sub(r"[^a-z0-9]+", "_", (device_model or "camera").lower()).strip("_") or "camera"
    camera = f"_camera{camera_id}" if camera_id else ""
    return re.sub(r"[^a-z0-9_]", "", f"{device}{camera}_{width}x{height}")


def focal_from_metadata(equivalent_mm: float, original_width: int, original_height: int) -> float:
    """Focal length in pixels from MotionCam's 35 mm-equivalent focal length and the full sensor size."""
    return float(equivalent_mm * math.hypot(original_width, original_height) / _EQUIVALENT_DIAGONAL_MM)


def builtin_dir() -> Path:
    return Path(__file__).parent / "profiles"


def user_dir() -> Path:
    return config_dir() / "cameras"


def load_profile(path: Path) -> CameraProfile:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return CameraProfile.model_validate(data)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"{path.name}: cannot read the profile ({exc})") from None
    except ValidationError as exc:
        known = [*CameraProfile.model_fields, *(f"match.{k}" for k in Match.model_fields)]
        problems = []
        for error in exc.errors():
            key = ".".join(str(p) for p in error["loc"] if not isinstance(p, int))
            hint = ""
            if error["type"] == "extra_forbidden":
                close = difflib.get_close_matches(key, known, n=1)
                hint = f" Did you mean {close[0]}?" if close else ""
            problems.append(f"{key}: {error['msg']}{hint}")
        raise ProfileError(f"{path.name}: " + "; ".join(problems)) from None


@functools.cache
def all_profiles() -> tuple[tuple[CameraProfile, bool], ...]:
    """(profile, is the user's) for every profile, the user's first."""
    found: list[tuple[CameraProfile, bool]] = []
    for folder, mine in ((user_dir(), True), (builtin_dir(), False)):
        if folder.is_dir():
            found += [(load_profile(p), mine) for p in sorted(folder.glob("*.toml"))]
    return tuple(found)


def find_sensor_profile(device_model: str | None, camera_id: str | None) -> CameraProfile | None:
    """Reuse physical sensor axes for an explicitly identified module, independent of size/FPS.

    A device-only match is not enough: front, main and tele cameras can face different directions.
    The source must name this module in its match or verified gravity-module list. Never carry over
    focal length, distortion, rolling-shutter readout or clock offsets from another recording mode.
    Equally specific conflicting axis measurements are left unresolved rather than guessed.
    """
    if not device_model or camera_id is None:
        return None
    model = device_model.strip().casefold()
    candidates: list[tuple[int, int, CameraProfile]] = []
    for profile, mine in all_profiles():
        wanted = profile.match
        if wanted.device_model is None or wanted.device_model.strip().casefold() != model:
            continue
        if wanted.camera_id is not None and wanted.camera_id != camera_id:
            continue
        if profile.gcsv_gravity_camera_ids and camera_id not in profile.gcsv_gravity_camera_ids:
            continue
        if wanted.camera_id != camera_id and camera_id not in profile.gcsv_gravity_camera_ids:
            continue
        candidates.append((int(wanted.camera_id == camera_id), int(mine), profile))
    candidates.sort(key=lambda candidate: candidate[:2], reverse=True)
    if not candidates:
        return None
    fields: dict[str, Axes] = {}
    sources: list[str] = []
    for field in ("gcsv_axes", "gcsv_gravity_axes", "gpmf_axes"):
        donors = [(specific, mine, p) for specific, mine, p in candidates if getattr(p, field) is not None]
        if not donors:
            continue
        priority = donors[0][:2]
        values = [getattr(p, field) for specific, mine, p in donors if (specific, mine) == priority]
        if any(value != values[0] for value in values[1:]):
            continue
        fields[field] = values[0]
        sources.append(donors[0][2].name)
    if not fields:
        return None
    return CameraProfile(
        name=f"{candidates[0][2].name}_sensors",
        description="Physical sensor axes from " + ", ".join(dict.fromkeys(sources)),
        match=Match(device_model=device_model, camera_id=camera_id),
        gcsv_gravity_camera_ids=(camera_id,) if "gcsv_gravity_axes" in fields else (),
        gcsv_axes=fields.get("gcsv_axes"),
        gcsv_gravity_axes=fields.get("gcsv_gravity_axes"),
        gpmf_axes=fields.get("gpmf_axes"),
    )


def find_profile(
    device_model: str | None, camera_id: str | None, width: int, height: int, fps: float
) -> CameraProfile | None:
    clip = {
        "device_model": device_model.strip().casefold() if device_model is not None else None,
        "camera_id": camera_id,
        "width": width,
        "height": height,
    }
    matches: list[tuple[int, int, CameraProfile]] = []
    for profile, mine in all_profiles():
        wanted = profile.match.model_dump(exclude_none=True)
        fps_ok = "fps" not in wanted or abs(wanted.pop("fps") - fps) < 0.05
        if "device_model" in wanted:
            wanted["device_model"] = wanted["device_model"].strip().casefold()
        if fps_ok and all(clip.get(k) == v for k, v in wanted.items()):
            matches.append((len(profile.match.model_dump(exclude_none=True)), int(mine), profile))
    if not matches:
        return find_sensor_profile(device_model, camera_id)
    matches.sort(key=lambda match: match[:2], reverse=True)
    best = matches[0][2]
    # Adding lens geometry must not discard trusted sensor timing or Horizon Lock mappings.
    # Only profiles matching this exact clip may contribute; explicit measurements always win.
    for _, _, fallback in matches[1:] if best.lens_calibration is not None else []:
        updates = {
            field: getattr(fallback, field)
            for field in (
                "readout_ms",
                "gcsv_axes",
                "gpmf_axes",
                "gcsv_sync_ms",
                "gpmf_sync_ms",
            )
            if getattr(best, field) is None and getattr(fallback, field) is not None
        }
        if best.gcsv_gravity_axes is None and fallback.gcsv_gravity_axes is not None:
            updates["gcsv_gravity_axes"] = fallback.gcsv_gravity_axes
            updates["gcsv_gravity_camera_ids"] = fallback.gcsv_gravity_camera_ids
        if updates:
            updates["description"] = f"{best.description}; gyro defaults from {fallback.name}"
            best = best.model_copy(update=updates)
    sensor = find_sensor_profile(device_model, camera_id)
    if sensor is not None:
        axes = {
            field: getattr(sensor, field)
            for field in ("gcsv_axes", "gcsv_gravity_axes", "gpmf_axes")
            if getattr(best, field) is None and getattr(sensor, field) is not None
        }
        if "gcsv_gravity_axes" in axes:
            axes["gcsv_gravity_camera_ids"] = sensor.gcsv_gravity_camera_ids
        if axes:
            axes["description"] = f"{best.description}; {sensor.description}"
            best = best.model_copy(update=axes)
    return best


def save_profile(profile: CameraProfile, force: bool = False) -> Path:
    folder = user_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{profile.name}.toml"
    if path.exists() and not force:
        raise ProfileError(f"{path} already exists (use --force to replace it)")
    path.write_text(tomli_w.dumps(profile.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
    all_profiles.cache_clear()
    return path


def delete_profile(name: str) -> None:
    """Remove a user profile and invalidate profile matching; built-in files are never touched."""
    if re.fullmatch(r"[a-z0-9][a-z0-9_]*", name) is None:
        raise ProfileError("Invalid camera profile name")
    (user_dir() / f"{name}.toml").unlink()
    all_profiles.cache_clear()
