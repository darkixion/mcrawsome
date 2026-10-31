"""The library: the clips in the interface's folders, with their ids, facts and readiness.

A folder's `.mcraw` clips and Direct Log videos are listed, and those in its subfolders one level down;
hidden files, sync tools' partial files and DaVinci Resolve's own folders are skipped, as are MCRAWsome's own
exports (a video with an output sidecar beside it) and any file whose real path, symlinks resolved, lies
outside the server's library folders.

A clip's id is made from its size and the first 16 MiB of its data, so the same clip at two paths has one
id (listed once, with every path) and a renamed clip keeps it. Ids and facts are remembered by path, size
and modification time in `state_dir()/library.json`, so a rescan of unchanged files, or one after a
restart, reads no clip data.

A file is `copying`, with no id, while it may still be changing: its size or modification time changed in
the last SETTLE_S seconds or while its start was being read, or it is a `.mcraw` without its frame index
that changed less than RECOVERED_WAIT_S ago. A file that does not open is `unreadable`, with the reason.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from mcrawsome.analysis.motion_summary import Movement
from mcrawsome.analysis.orientation import OrientationHint
from mcrawsome.analysis.orientation import check as check_orientation
from mcrawsome.analysis.orientation import check_recorded as check_recorded_orientation
from mcrawsome.camera.profile import Axes, ProfileError, find_profile
from mcrawsome.camera.recorded import recorded_camera
from mcrawsome.colour.encoding import colour_labels
from mcrawsome.colour.white_balance import as_shot_values
from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.io.describe import dropped_frames, duplicated_frames
from mcrawsome.io.directlog import (
    NotLogVideoError,
    aperture_from_minf,
    camera_from_minf,
    is_video_input,
    probe_direct_log,
    read_gyro,
    require_log_video,
    telemetry_summary,
)
from mcrawsome.library import SETTLE_S as SETTLE_S
from mcrawsome.library.formats import IGNORED, SUFFIXES, scanned_folder
from mcrawsome.output.ffmpeg import find_ffmpeg, frame_rate
from mcrawsome.output.sidecar import quick_hash, sidecar_path
from mcrawsome.paths import state_dir
from mcrawsome.recipe.clip_layer import ClipLayer, path_for, read_layer
from mcrawsome.recipe.load import RecipeError, build_recipe, load_toml, lower_layer_paths
from mcrawsome.recipe.raw import saved_rotation
from mcrawsome.recipe.schema import Recipe

RECOVERED_WAIT_S = 600.0  # a .mcraw without its frame index waits this long unchanged
MOTION_BUDGET_S = 1.0  # time a scan spends measuring movement before leaving the rest to the next
_RECORD_VERSION = 7

log = logging.getLogger(__name__)

Kind = Literal["raw", "log"]
Status = Literal["ready", "copying", "unreadable"]
Badge = Literal["recovered", "has-settings", "dropped-frames"]
Stamp = tuple[int, int]  # size, modification time (ns)


class CaptureFacts(BaseModel):
    gps: bool = Field(False, description="A valid recorded GPS fix is available")
    position: tuple[float, float] | None = Field(
        None, description="The recorded GPS fix: latitude and longitude in degrees"
    )
    frame_count: int | None = None
    measured_fps: float | None = None
    focal_length_mm: float | None = None
    aperture: float | None = None
    iso: float | None = None
    exposure_time_ns: float | None = None
    sensor_bits: int | None = None
    white_level: float | None = None
    black_levels: tuple[float, float, float, float] | None = None
    missing_frames: int | None = None
    duplicated_frames: int | None = None
    as_shot_temperature: float | None = None
    as_shot_tint: float | None = None
    movement: Movement | None = Field(
        None, description="The movement covering most of the clip, measured from its gyro log"
    )
    shake_blur_px: float | None = Field(
        None, description="Shake blur (pixels) that 95% of frames stay under, from the gyro log and shutter"
    )
    blurred_s: float | None = Field(
        None, description="Seconds of sustained shake blur (20 px or more for half a second or longer)"
    )


class ClipSummary(CaptureFacts):
    """A clip as the library lists it. The facts are None while it is copying or when it is unreadable."""

    id: str | None = Field(description="The clip's fingerprint; none while copying or if it cannot be read")
    path: Path = Field(description="The first of `paths` in sorted order")
    paths: list[Path] = Field(description="Every path in the library folders with this clip, sorted")
    canonical_paths: list[Path] = Field(
        default_factory=list, description="Resolved sources for these library paths"
    )
    name: str
    kind: Kind
    duration_s: float | None = None
    width: int | None = None  # RAW: the recorded photosites; video: as displayed
    height: int | None = None
    display_rotation: Literal[0, 90, 180, 270] = Field(
        0, description="Saved clockwise RAW display turn; width and height remain recorded sensor dimensions"
    )
    fps: float | None = Field(None, description="The frame rate an export is labelled with")
    captured: datetime | None = None
    camera: str | None = None
    status: Status
    badges: list[Badge] = Field(default_factory=list)
    error: str | None = None
    rating: int = Field(default=0, ge=0, le=5)
    place: str | None = Field(
        None, description="The nearest city to the recorded GPS fix (offline), when one lies within 50 km"
    )
    place_km: float | None = Field(None, description="How far the recorded GPS fix is from `place`")
    details_text: str = Field(
        "",
        description="The clip's own details a search matches: title, description, keywords, notes, "
        "scene, shot, take, location and camera label, one per line",
    )
    orientation_hint: OrientationHint | None = None
    gyro: bool = Field(False, description="A matching gyro log or embedded telemetry is available")


class ClipDetails(ClipSummary):
    """One clip's facts and readiness, and its own Recipe layer (the one beside `path`)."""

    dropped_frames: int
    filled_frames: int = Field(
        description="Frames an export repeats for the dropped ones (output.fill_dropped)"
    )
    recovered_note: str | None = None
    camera_profile: str | None = Field(None, description="The matching camera profile's name, if any")
    recipe: ClipLayer
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Original container and first-frame metadata, or video/device facts"
    )


class LibraryView(BaseModel):
    clips: list[ClipSummary]
    missing_folders: list[Path] = Field(description="Library folders that are not there (an unplugged drive)")


class LibraryChange(BaseModel):
    """What a rescan changed. A client removes the `removed` ids and the id-less (copying) entries at
    `removed_paths`, then adds or replaces `clips` (by id, or by path for a copying one). With `reload`,
    changes were dropped for a slow client, so it must read the whole library again."""

    clips: list[ClipSummary]
    removed: list[str]
    removed_paths: list[Path]
    missing_folders: list[Path]
    reload: bool = False


class _Facts(CaptureFacts):
    """What a scan read from one file, remembered by its size and modification time."""

    size: int
    mtime_ns: int
    first_16mib: str
    id: str
    kind: Kind
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    captured: datetime | None = None
    camera: str | None = None
    recovered: bool = False
    dropped: int = 0
    error: str | None = None
    recorded_orientation: int | None = None
    device_model: str | None = None
    camera_id: str | None = None
    gyro: bool = False
    # RAW: whether the recording carries its own motion (MotionCam 5.1 and later), and the gravity axes its
    # calibration gives; None in records from before this was read.
    recorded_motion: bool | None = None
    recorded_gravity: Axes | None = None
    # The gyro log `movement` was measured from: a .gcsv's size and modification time, or (0, 0) for
    # telemetry inside a Direct Log video or a RAW recording; None when nothing was measured.
    motion_source: tuple[int, int] | None = None
    # The gravity check's orientation hint (None: no hint), and what it was worked out from: the .gcsv's
    # size and modification time, the saved display turn, and the profile's gravity axes and signs.
    orientation_source: tuple[int, int, int, tuple[int, int, int], tuple[int, int, int]] | None = None
    orientation_hint: OrientationHint | None = None


@dataclass(frozen=True)
class _Found:
    listed: Path  # the path in the folder, as the library shows it
    real: Path  # symlinks resolved
    facts: _Facts | None  # None: not read (copying), or it could not be read (`error`)
    copying: bool = False
    error: str | None = None


class ChangingError(Exception):
    """The file changed while it was being read."""


def inside(path: Path, roots: Iterable[Path]) -> Path | None:
    """`path` with symlinks resolved, when it lies in one of `roots` (resolved folders); else None. A set
    of roots is looked up by the path's own folders, so a scan of many opened files is not quadratic."""
    resolved = path.resolve()
    wanted = roots if isinstance(roots, (set, frozenset)) else set(roots)
    return resolved if resolved in wanted or not wanted.isdisjoint(resolved.parents) else None


def _stamp(path: Path) -> Stamp:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _id(size: int, first_16mib: str) -> str:
    return hashlib.sha256(f"{size}:{first_16mib}".encode()).hexdigest()[:16]


def clip_id(path: Path) -> str:
    """The id of the clip at `path`: from its size and the sha256 of its first 16 MiB (what an export's
    manifest records), 16 hex digits. ChangingError when the file changed while it was read."""
    before = _stamp(path)
    digest = quick_hash(path)
    if _stamp(path) != before:
        raise ChangingError(f"{path.name} changed while it was read")
    return _id(before[0], digest)


def _kind(path: Path) -> Kind:
    return "log" if is_video_input(path) else "raw"


def recording_columns(path: Path) -> dict[str, str]:
    """A recording's duration, resolution, frame rate and camera as text, for a file manager's columns, as
    the library reads them; empty when it cannot be read. Durations are H:MM:SS, so they sort as text."""
    try:
        facts = _read_facts(path)
    except Exception:  # a recording that cannot be read only has empty columns
        return {}
    seconds = round(float(facts.get("duration_s") or 0))
    columns = {"duration": f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"}
    if facts.get("width") and facts.get("height"):
        columns["resolution"] = f"{facts['width']}x{facts['height']}"
    if facts.get("fps"):
        columns["frame_rate"] = f"{round(float(facts['fps']), 3):g} fps"
    if facts.get("camera"):
        columns["camera"] = str(facts["camera"])
    return columns


def _read_facts(path: Path) -> dict[str, Any]:
    """The clip's facts from its metadata (no frame is decoded)."""
    if is_video_input(path):
        ffmpeg, ffprobe = find_ffmpeg()
        info = probe_direct_log(ffprobe, path)
        require_log_video(info)
        gyro, device = read_gyro(ffmpeg, info)
        model, equivalent = camera_from_minf(device.get("MINF", ""))
        width, height = info.display_size
        motion: dict[str, object] = {}
        if gyro is not None:
            from mcrawsome.analysis.motion_insights import video_motion

            try:
                motion = video_motion(gyro, device, info, None).facts()
            except Exception as exc:  # a bad profile or odd telemetry must not make the clip unreadable
                log.info("No movement for %s: %s", path.name, exc)
        return {
            **motion, "motion_source": (0, 0) if gyro is not None else None,
            "kind": "log", "duration_s": info.duration_s, "width": width, "height": height,
            "fps": float(info.fps), "frame_count": info.frame_count, "captured": info.created,
            "gyro": gyro is not None, "camera": model,
            "focal_length_mm": equivalent, "aperture": aperture_from_minf(device.get("MINF", "")),
            "sensor_bits": info.bit_depth,
        }  # fmt: skip
    with Clip(path) as clip:
        first, meta, measured = clip.frame_metadata(0), clip.metadata, clip.fps
        balance = as_shot_values(clip)
        stamps = clip.timestamps_ns
        named = meta.manufacturer and meta.build_model
        return {
            "kind": "raw",
            "as_shot_temperature": balance["temperature"],
            "as_shot_tint": balance["tint"],
            "duration_s": float(stamps[-1] - stamps[0]) / 1e9 + 1 / measured,
            "width": first.width,
            "height": first.height,
            "fps": float(frame_rate(measured)),
            "captured": datetime.fromtimestamp(first.received_ms / 1000, UTC) if first.received_ms else None,
            "camera": f"{meta.manufacturer} {meta.build_model}" if named else meta.device_model,
            "gps": meta.gps is not None,
            "position": (meta.gps[0], meta.gps[1]) if meta.gps is not None else None,
            "frame_count": clip.frame_count,
            "measured_fps": measured,
            "focal_length_mm": meta.focal_lengths[0] if meta.focal_lengths else None,
            "aperture": meta.apertures[0] if meta.apertures else None,
            "iso": first.iso,
            "exposure_time_ns": first.exposure_time_ns,
            "sensor_bits": max(1, round(first.white_level).bit_length()),
            "white_level": first.white_level,
            "black_levels": first.black_level,
            "missing_frames": dropped_frames(stamps),
            "duplicated_frames": duplicated_frames(stamps),
            "recovered": clip.recovered,
            "dropped": dropped_frames(stamps),
            "recorded_orientation": first.orientation,
            "device_model": meta.device_model,
            "camera_id": meta.camera_id,
            "recorded_motion": clip.has_recorded_motion,
            "recorded_gravity": camera.gravity_axes
            if clip.has_recorded_motion and (camera := recorded_camera(meta, clip.frame_timing(0)))
            else None,
        }


@functools.lru_cache(maxsize=1024)
def _nearby(latitude: float, longitude: float) -> tuple[str | None, float | None]:
    from mcrawsome.library.places import nearest

    place = nearest(latitude, longitude)
    return (place.label, place.distance_km) if place is not None else (None, None)


def _place(position: tuple[float, float] | None) -> tuple[str | None, float | None]:
    """The nearest city's name and distance, from a cache keyed to about 100 m."""
    return _nearby(round(position[0], 3), round(position[1], 3)) if position is not None else (None, None)


def _candidates(folder: Path) -> list[Path]:
    """The clips in `folder` and its subfolders one level down, excluding incomplete files."""

    def visible(path: Path) -> bool:
        return not any(fnmatch(path.name, pattern) for pattern in IGNORED)

    def clips(where: Path) -> list[Path]:
        return [
            path
            for path in where.iterdir()
            if visible(path)
            and path.suffix.lower() in SUFFIXES
            and path.is_file()
            and not (is_video_input(path) and sidecar_path(path).exists())  # one of MCRAWsome's exports
        ]

    try:
        found = clips(folder)
        for sub in folder.iterdir():
            if scanned_folder(sub) and sub.is_dir():
                found += clips(sub)
    except OSError as exc:  # a folder going while it is read
        log.warning("reading %s failed: %s", folder, exc)
        return []
    return sorted(found)


def clip_recipe(clip: Path, layer: ClipLayer) -> Recipe:
    """The clip's Recipe: the defaults, the folder's Recipe and the clip's own layer. A layer that does not
    validate is left out, as is a lower layer that does not."""
    try:
        lower = [load_toml(path) for path in lower_layer_paths(clip)]
    except (RecipeError, OSError):
        lower = []
    if layer.error is None:
        with contextlib.suppress(RecipeError):
            return build_recipe([*lower, layer.values])
    with contextlib.suppress(RecipeError):
        return build_recipe(lower)
    return build_recipe([])


def _missing(path: Path) -> McError:
    return McError(f"{path.name} is no longer in the library folders", code="clip-missing")


def _profile(model: str | None, camera_id: str | None, width: int, height: int, fps: float) -> str | None:
    try:
        profile = find_profile(model, camera_id, width, height, fps)
    except ProfileError as exc:
        log.warning("the camera profiles could not be read: %s", exc)
        return None
    return profile.name if profile is not None else None


def _readiness(path: Path, clip: ClipSummary) -> dict[str, Any]:
    """What ClipDetails adds to a ready clip's summary, read from the clip now."""
    if clip.kind == "log":
        ffmpeg, ffprobe = find_ffmpeg()
        info = probe_direct_log(ffprobe, path)
        gyro, device = read_gyro(ffmpeg, info)
        model, _ = camera_from_minf(device.get("MINF", ""))
        metadata = (
            asdict(info)
            | colour_labels(info.encoding, info.encoding_source)
            | {
                "path": str(info.path),
                "fps": str(info.fps),
                "created": info.created.isoformat() if info.created is not None else None,
            }
        )
        return {
            "metadata": {"Video": metadata, "Device": device, "Telemetry": telemetry_summary(gyro)},
            "gyro": gyro is not None,
            "camera": model or clip.camera,
            "camera_profile": _profile(model, None, info.width, info.height, float(info.fps)),
        }
    with Clip(path) as opened:
        first, meta = opened.frame_metadata(0), opened.metadata
        # The recording's own lens calibration takes the place of a camera profile when stabilising.
        calibrated = opened.has_recorded_motion and recorded_camera(meta, opened.frame_timing(0)) is not None
        return {
            "metadata": {
                "Container": opened.raw_metadata,
                "First frame": opened.raw_frame_metadata(0),
                "White balance": as_shot_values(opened),
            },
            "gyro": opened.has_recorded_motion or opened.gyro_log_path() is not None,
            "dropped_frames": dropped_frames(opened.timestamps_ns),
            "recovered_note": opened.recovery_note,
            "camera_profile": "Recorded in the clip"
            if calibrated
            else _profile(meta.device_model, meta.camera_id, first.width, first.height, opened.fps),
        }


class Library:
    """The library's scanner. It keeps the last scan (`view`) to say what the next one changed. Safe to
    use from several threads: scans take turns."""

    def __init__(self, clock: Callable[[], float] = time.time, *, record: Path | None = None) -> None:
        self.clock = clock
        self.record = record if record is not None else state_dir() / "library.json"
        self.view = LibraryView(clips=[], missing_folders=[])
        self._facts = self._load()  # by real path
        self._rejected: dict[Path, Stamp] = {}  # unchanged non-Log videos need no repeat probing
        self._failed: dict[str, _Facts] = {}  # failed reads are shared only within this scan
        # by real path: its stamp, and when the stamp was last seen to change
        self._seen: dict[Path, tuple[Stamp, float]] = {}
        self._changed = False  # the facts changed during this scan: library.json is written
        self._lock = threading.Lock()
        # Movement is measured from gyro logs for at most this long per scan (at least one clip), so a
        # large library lists quickly; `pending_motion` asks the watcher to scan again for the rest.
        self.motion_budget_s = MOTION_BUDGET_S
        self.pending_motion = False
        self._motion_deadline = 0.0
        self._motion_measured = 0

    def _load(self) -> dict[str, _Facts]:
        try:
            record = json.loads(self.record.read_text())
            if record.get("version") != _RECORD_VERSION:
                return {}
            return {
                path: facts
                for path, values in record["files"].items()
                if (facts := _Facts.model_validate(values)).error is None
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ValidationError):
            return {}

    def _save(self) -> None:
        files = {path: facts.model_dump(mode="json") for path, facts in sorted(self._facts.items())}
        self.record.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self.record.parent, prefix=f".{self.record.name}.", delete=False
        ) as part:
            json.dump({"version": _RECORD_VERSION, "files": files}, part, indent=1)
        os.replace(part.name, self.record)

    def scan(self, folders: Sequence[Path], roots: Sequence[Path] | None = None) -> LibraryView:
        """Scan `folders`; a file is listed only when its real path lies in `roots` (by default the
        folders themselves)."""
        return self.refresh(folders, roots)[0]

    def refresh(
        self, folders: Sequence[Path], roots: Sequence[Path] | None = None
    ) -> tuple[LibraryView, LibraryChange | None]:
        """Scan, and say what changed since the last scan (None when nothing did)."""
        with self._lock:
            before = self.view
            self.view = self._scan(folders, [root.resolve() for root in (roots or folders)])
            return self.view, _difference(before, self.view)

    def scan_files(self, files: Sequence[Path]) -> LibraryView:
        """Read only explicitly opened files; never enumerate their neighbouring folders."""
        with self._lock:
            self.view = self._scan([], [path.resolve() for path in files], files=files)
            return self.view

    def find(self, clip_id: str) -> ClipSummary | None:
        """The clip with this id as the last scan found it."""
        return next((clip for clip in self.view.clips if clip.id == clip_id), None)

    def first_16mib(self, path: Path) -> str | None:
        """The hash of the clip's first 16 MiB, when the last scan read it."""
        facts = self._facts.get(str(path.resolve()))
        return facts.first_16mib if facts is not None else None

    def details(self, clip: ClipSummary) -> ClipDetails:
        """The clip's details, read now. McError("clip-missing") when it has gone since the scan."""
        path = clip.path
        if not path.is_file():
            raise _missing(path)
        layer = read_layer(path, self.first_16mib(path))
        recipe = clip_recipe(path, layer)
        known: dict[str, Any] = {"gyro": False, "dropped_frames": 0, "recovered_note": None}
        try:
            if clip.status == "ready":
                known |= _readiness(path, clip)
        except FileNotFoundError:
            raise _missing(path) from None
        except Exception as exc:  # it opened at the scan: say why it does not now
            if not path.is_file():
                raise _missing(path) from None
            known["error"] = str(exc)
            known["status"] = "unreadable"
        filled = known["dropped_frames"] if recipe.output.fill_dropped else 0
        hint = self._orientation(path, self._facts.get(str(path.resolve())))
        return ClipDetails.model_validate(
            clip.model_dump()
            | known
            | {
                "filled_frames": filled,
                "recipe": layer,
                "orientation_hint": hint,
                "display_rotation": recipe.develop.rotation if clip.kind == "raw" else 0,
            }
        )

    def _orientation(
        self, path: Path, facts: _Facts | None, rotation: int | None = None, *, remember: bool = False
    ) -> OrientationHint | None:
        """The gravity check's hint; `rotation`, the saved display turn, when the caller has read it.

        A hint worked out from the same gyro log, turn and gravity axes as the facts' is reused, so a listing
        after a restart reads no gyro logs; with `remember` (a scan, which saves the record), a new one is
        kept in the facts."""
        if facts is None or facts.kind != "raw" or facts.error is not None:
            return None
        try:
            if facts.recorded_motion:  # the recording's own accelerometer and calibration
                axes = facts.recorded_gravity
                if axes is None:
                    return None
                turn = saved_rotation(path, facts.first_16mib) if rotation is None else rotation
                source = (facts.size, facts.mtime_ns, turn, axes.axes, axes.signs)
                if facts.orientation_source == source:
                    return facts.orientation_hint
                hint = check_recorded_orientation(path, axes, facts.recorded_orientation, turn)
            else:
                profile = find_profile(
                    facts.device_model,
                    facts.camera_id,
                    facts.width or 0,
                    facts.height or 0,
                    facts.measured_fps or 0,
                )
                if profile is None or profile.gcsv_gravity_axes is None:
                    return None
                if profile.gcsv_gravity_camera_ids and facts.camera_id not in profile.gcsv_gravity_camera_ids:
                    return None
                axes = profile.gcsv_gravity_axes
                turn = saved_rotation(path, facts.first_16mib) if rotation is None else rotation
                log = path.with_suffix(".gcsv")
                source = (*_stamp(log), turn, axes.axes, axes.signs)
                if facts.orientation_source == source:
                    return facts.orientation_hint
                hint = check_orientation(log, axes, facts.recorded_orientation, turn)
            key = str(path.resolve())
            current = self._facts.get(key)
            if (
                remember
                and current is not None
                and (current.size, current.mtime_ns) == (facts.size, facts.mtime_ns)
            ):
                self._facts[key] = current.model_copy(
                    update={"orientation_source": source, "orientation_hint": hint}
                )
                self._changed = True
            return hint
        except (ProfileError, RecipeError, OSError):
            return None

    def _scan(self, folders: Sequence[Path], roots: list[Path], *, files: Sequence[Path] = ()) -> LibraryView:
        now = self.clock()
        missing = [folder for folder in folders if not folder.is_dir()]
        found: list[_Found] = []
        seen: dict[Path, tuple[Stamp, float]] = {}
        listed: set[Path] = set()
        self._changed = False
        self.pending_motion = False
        self._motion_deadline = time.monotonic() + self.motion_budget_s
        self._motion_measured = 0
        self._failed = {}
        candidates = [path for folder in folders if folder not in missing for path in _candidates(folder)]
        wanted = frozenset(roots)
        for path in [*candidates, *files]:
            if path in listed:
                continue
            listed.add(path)
            real = inside(path, wanted)
            if real is None:
                continue
            entry = self._consider(path, real, now, seen)
            if entry is not None:
                found.append(entry)
        self._seen = seen
        kept = {str(entry.real) for entry in found if entry.facts is not None}
        absent = [folder.resolve() for folder in missing]
        for key in list(self._facts):  # a clip on an unplugged drive keeps its facts for its return
            if key not in kept and not any(Path(key).is_relative_to(folder) for folder in absent):
                del self._facts[key]
                self._changed = True
        clips = self._summaries(found)  # orientation hints worked out afresh change the facts too
        if self._changed:
            self._save()
        return LibraryView(clips=clips, missing_folders=missing)

    def _consider(
        self, path: Path, real: Path, now: float, seen: dict[Path, tuple[Stamp, float]]
    ) -> _Found | None:
        """The file as found now; None when it has gone."""
        try:
            stamp = _stamp(real)
        except OSError:
            return None
        if self._rejected.get(real) == stamp:
            return None
        before = self._seen.get(real)
        changed = before[1] if before is not None and before[0] == stamp else now if before else -1.0
        seen[real] = (stamp, changed)
        still = now - max(changed, stamp[1] / 1e9)  # how long it has been unchanged
        if still < SETTLE_S:
            return _Found(path, real, None, copying=True)
        facts = self._facts.get(str(real)) or self._failed.get(str(real))
        old_metadata = (
            facts is not None
            and facts.error is None
            and (
                facts.frame_count is None
                or (facts.kind == "raw" and facts.duplicated_frames is None)
                or (facts.kind == "log" and facts.gyro and facts.motion_source is None)
                or (facts.kind == "raw" and facts.gps and facts.position is None)
                or (facts.kind == "raw" and facts.recorded_motion is None)
            )
        )
        if facts is None or (facts.size, facts.mtime_ns) != stamp or old_metadata:
            try:
                facts = self._read(real, stamp)
            except ChangingError:
                seen[real] = (_stamp_or(real, stamp), now)
                return _Found(path, real, None, copying=True)
            except FileNotFoundError:
                return None
            except NotLogVideoError:
                self._rejected[real] = stamp
                return None
            except OSError as exc:  # it cannot be read at all (permissions, a failing drive)
                return _Found(path, real, None, error=str(exc))
            if facts.error is None:
                self._changed |= self._facts.get(str(real)) != facts
                self._facts[str(real)] = facts
            else:
                self._changed |= self._facts.pop(str(real), None) is not None
                self._failed[str(real)] = facts
        if facts.error is None and facts.kind == "raw":
            facts = self._with_motion(path, real, facts)
        waiting = facts.recovered and still < RECOVERED_WAIT_S  # perhaps still being copied
        return _Found(path, real, facts, copying=waiting)

    def _with_motion(self, path: Path, real: Path, facts: _Facts) -> _Facts:
        """Measure a RAW clip's movement again when the gyro log beside it appeared, changed or went. Motion
        recorded in the clip is measured once: it changes only with the clip, which reads its facts afresh."""
        beside = path.with_suffix(".gcsv")
        try:
            current = (0, 0) if facts.recorded_motion else _stamp(beside) if beside.is_file() else None
        except OSError:
            current = None
        if facts.motion_source == current:
            return facts
        if current is not None and self._motion_measured and time.monotonic() > self._motion_deadline:
            self.pending_motion = True  # measured on a later scan; the clip is listed meanwhile
            return facts
        self._motion_measured += 1
        measured: dict[str, object] = {"movement": None, "shake_blur_px": None, "blurred_s": None}
        if current is not None:
            from mcrawsome.analysis.motion_insights import load

            try:
                motion = load(path, every_frame=False)
                if motion is not None:
                    measured |= motion.facts()
            except Exception as exc:  # an odd log must not stop the scan
                log.info("No movement for %s: %s", path.name, exc)
        updated = facts.model_copy(update={**measured, "motion_source": current})
        self._facts[str(real)] = updated
        self._changed = True
        return updated

    def _read(self, real: Path, stamp: Stamp) -> _Facts:
        """Read the file's id and facts; ChangingError when it changed meanwhile."""
        cached = self._facts.get(str(real))
        digest = (
            cached.first_16mib if cached and (cached.size, cached.mtime_ns) == stamp else quick_hash(real)
        )
        try:
            values = _read_facts(real)
        except (FileNotFoundError, NotLogVideoError):
            raise
        except Exception as exc:  # one odd file must not stop the scan of the others
            values = {"kind": _kind(real), "error": str(exc) or type(exc).__name__}
        if _stamp(real) != stamp:
            raise ChangingError(f"{real.name} changed while it was read")
        return _Facts(
            size=stamp[0], mtime_ns=stamp[1], first_16mib=digest, id=_id(stamp[0], digest), **values
        )

    def _summaries(self, found: list[_Found]) -> list[ClipSummary]:
        from mcrawsome.library.annotations import details_for_summary

        clips: list[ClipSummary] = []
        same: dict[str, list[_Found]] = {}
        for entry in found:
            if entry.facts is None or entry.copying:
                clips.append(
                    ClipSummary(
                        id=None, path=entry.listed, paths=[entry.listed], name=entry.listed.name,
                        kind=_kind(entry.listed), status="copying" if entry.copying else "unreadable",
                        error=entry.error,
                    )
                )  # fmt: skip
            else:
                same.setdefault(entry.facts.id, []).append(entry)
        for entries in same.values():
            entries.sort(key=lambda entry: entry.listed)
            first = entries[0]
            facts = first.facts
            assert facts is not None
            badges: list[Badge] = []
            if facts.recovered:
                badges.append("recovered")
            if path_for(first.listed, facts.first_16mib).exists():
                badges.append("has-settings")
            if facts.dropped:
                badges.append("dropped-frames")
            rotation: Literal[0, 90, 180, 270] | None = 0
            if facts.kind == "raw":
                try:
                    rotation = saved_rotation(first.listed, facts.first_16mib)
                except (RecipeError, OSError):
                    rotation = None  # shown unturned, with no orientation hint
            rating, words = details_for_summary(first.real, facts.id)
            clips.append(
                ClipSummary(
                    id=facts.id,
                    rating=rating,
                    details_text=words,
                    place=(nearby := _place(facts.position))[0],
                    place_km=nearby[1],
                    path=first.listed,
                    paths=[entry.listed for entry in entries],
                    canonical_paths=sorted({entry.real for entry in entries}),
                    name=first.listed.name,
                    status="unreadable" if facts.error is not None else "ready",
                    badges=badges,
                    display_rotation=rotation or 0,
                    orientation_hint=(
                        self._orientation(first.listed, facts, rotation, remember=True)
                        if rotation is not None
                        else None
                    ),
                    gyro=(
                        bool(facts.recorded_motion) or first.listed.with_suffix(".gcsv").is_file()
                        if facts.kind == "raw"
                        else facts.gyro
                    ),
                    **facts.model_dump(
                        include={
                            *CaptureFacts.model_fields,
                            "kind",
                            "duration_s",
                            "width",
                            "height",
                            "fps",
                            "captured",
                            "camera",
                            "error",
                        }
                    ),
                )
            )
        return sorted(clips, key=lambda clip: clip.path)


def _stamp_or(path: Path, fallback: Stamp) -> Stamp:
    try:
        return _stamp(path)
    except OSError:
        return fallback


def _key(clip: ClipSummary) -> str:
    return clip.id if clip.id is not None else f"path:{clip.path}"


def _difference(before: LibraryView, after: LibraryView) -> LibraryChange | None:
    old = {_key(clip): clip for clip in before.clips}
    new = {_key(clip): clip for clip in after.clips}
    changed = [clip for key, clip in new.items() if old.get(key) != clip]
    gone = [clip for key, clip in old.items() if key not in new]
    if not changed and not gone and before.missing_folders == after.missing_folders:
        return None
    return LibraryChange(
        clips=changed,
        removed=[clip.id for clip in gone if clip.id is not None],
        removed_paths=[clip.path for clip in gone if clip.id is None],
        missing_folders=after.missing_folders,
    )


def scan(folders: Sequence[Path], roots: Sequence[Path] | None = None) -> list[ClipSummary]:
    """The clips in `folders` (see Library.scan)."""
    return Library().scan(folders, roots).clips
