"""Neutral Library thumbnails, retaining the saved orientation but excluding creative settings.

The steadiest frame of the middle half (the least shake blur in the gyro log), or the middle frame without
a log, is developed at half size and displayed at 320 pixels wide, with camera white balance, zero
exposure adjustment and the neutral display transform.
Thumbnails are cached in `cache_dir()/thumbs/<id>-<fingerprint>.jpg`; the version invalidates earlier
styled images. The cache cap removes the least recently used thumbnails first. A card's skim, SKIM_FRAMES
pictures spread through the clip and made the same way, is cached likewise in `skims`.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import threading
from pathlib import Path

from mcrawsome.errors import McError
from mcrawsome.io.clip import remosaic_enabled
from mcrawsome.io.directlog import is_video_input
from mcrawsome.paths import cache_dir
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import RecipeError
from mcrawsome.recipe.raw import saved_rotation
from mcrawsome.recipe.schema import DevelopParams, MotionParams, Recipe
from mcrawsome.recipe.source import saved_video_recipe

WIDTH = 320
CAP_BYTES = 500 * 1024 * 1024
SKIM_FRAMES = 10  # pictures across a clip, for skimming its card
_VERSION = 6  # the neutral display rendering without looks, from the steadiest frame
_making = threading.Semaphore(2)  # thumbnails made at once (each develops a frame)
_skimming = threading.Semaphore(1)  # skims made at once (each develops SKIM_FRAMES frames)


def _log_stamp(clip: Path) -> list[int] | None:
    """The gyro log beside a RAW clip (size and modification time): it chooses the frame shown."""
    with contextlib.suppress(OSError):
        stat = clip.with_suffix(".gcsv").stat()
        return [stat.st_size, stat.st_mtime_ns]
    return None


def fingerprint(recipe: Recipe, quad_bayer_remosaic: bool | None = None, clip: Path | None = None) -> str:
    """What a thumbnail depends on besides the clip: the settings a preview frame is made with (no
    stabilisation), and the gyro log that picks the frame."""
    settings = recipe.model_dump(mode="json", include={"develop", "camera"})
    settings |= {"fill_dropped": recipe.output.fill_dropped, "version": _VERSION}
    if clip is not None:
        settings["gyro"] = _log_stamp(clip)
    settings["quad_bayer_remosaic"] = (
        remosaic_enabled() if quad_bayer_remosaic is None else quad_bayer_remosaic
    )
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]


def _missing(clip: Path) -> McError:
    return McError(f"{clip.name} is no longer in the library folders", code="clip-missing")


def _cached(path: Path) -> bytes | None:
    try:
        jpeg = path.read_bytes()
    except FileNotFoundError:
        return None
    with contextlib.suppress(FileNotFoundError):
        os.utime(path)  # used now: the last to be removed
    return jpeg


def _shown(clip: Path, count: int) -> int:
    """The steadiest frame of the middle half, by the gyro log's shake blur; else (or for a clip under a
    second) the middle frame."""
    from mcrawsome.analysis.motion_insights import load

    if count < 24:
        return count // 2
    try:
        motion = load(clip, every_frame=False)
        index = motion.steadiest() if motion is not None else None
    except Exception:  # a thumbnail never fails for want of a gyro log
        return count // 2
    if motion is None or index is None or len(motion.centres) == 0:
        return count // 2
    return min(count - 1, round(index * count / len(motion.centres)))


def _render(clip: Path, recipe: Recipe, quad_bayer_remosaic: bool) -> bytes:
    """The shown frame as a preview makes it, at most WIDTH wide."""
    try:
        with PreviewSession(
            clip, recipe, PreviewSettings(width=WIDTH, quad_bayer_remosaic=quad_bayer_remosaic)
        ) as session:
            return session.frame(_shown(clip, session.frame_count))
    except FileNotFoundError:
        raise _missing(clip) from None
    except McError as exc:
        if exc.code == "input-missing" or not clip.is_file():
            raise _missing(clip) from None
        raise


def _store(path: Path, jpeg: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{secrets.token_hex(4)}.part")
    try:
        part.write_bytes(jpeg)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _trim(folder: Path, cap_bytes: int, keep: Path) -> None:
    """Remove the least recently used thumbnails until the folder holds at most `cap_bytes`."""
    files = []
    for path in folder.glob(f"*{keep.suffix}"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime_ns, stat.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= cap_bytes:
            break
        if path != keep:
            path.unlink(missing_ok=True)
            total -= size


def _neutral(clip: Path, first_16mib: str | None) -> Recipe:
    """A thumbnail's settings: the saved display turn, nothing creative."""
    if not clip.is_file():
        raise _missing(clip)
    rotation = 0
    with contextlib.suppress(RecipeError, OSError):
        rotation = saved_rotation(clip, first_16mib)
    recipe = Recipe(
        develop=DevelopParams(exposure=0, rotation=rotation),
        motion=MotionParams(stabilise="off"),
    )
    if is_video_input(clip):
        try:
            recipe = saved_video_recipe(clip, rotation, first_16mib)
        except (RecipeError, OSError) as exc:
            raise McError(str(exc), code="recipe-invalid") from None
    return recipe


def thumbnail(clip: Path, clip_id: str, first_16mib: str | None = None, cap_bytes: int = CAP_BYTES) -> bytes:
    """The neutral thumbnail, retaining the saved display turn but excluding creative settings."""
    recipe = _neutral(clip, first_16mib)
    folder = cache_dir() / "thumbs"
    quad_bayer_remosaic = remosaic_enabled()
    path = folder / f"{clip_id}-{fingerprint(recipe, quad_bayer_remosaic, clip)}.jpg"
    if (jpeg := _cached(path)) is not None:
        return jpeg
    with _making:
        if (jpeg := _cached(path)) is not None:  # made by another request meanwhile
            return jpeg
        jpeg = _render(clip, recipe, quad_bayer_remosaic)
        _store(path, jpeg)
        _trim(folder, cap_bytes, keep=path)
    return jpeg


def _render_skim(clip: Path, recipe: Recipe, quad_bayer_remosaic: bool) -> list[bytes]:
    """The middle frame of each SKIM_FRAMES-th of the clip, as a thumbnail is made; each frame once."""
    try:
        with PreviewSession(
            clip, recipe, PreviewSettings(width=WIDTH, quad_bayer_remosaic=quad_bayer_remosaic)
        ) as session:
            count = session.frame_count
            shown = [
                min(count - 1, (2 * part + 1) * count // (2 * SKIM_FRAMES)) for part in range(SKIM_FRAMES)
            ]
            made = {index: session.frame(index) for index in sorted(set(shown))}
    except FileNotFoundError:
        raise _missing(clip) from None
    except McError as exc:
        if exc.code == "input-missing" or not clip.is_file():
            raise _missing(clip) from None
        raise
    return [made[index] for index in shown]


def skim(clip: Path, clip_id: str, first_16mib: str | None = None, cap_bytes: int = CAP_BYTES) -> list[bytes]:
    """SKIM_FRAMES JPEGs spread evenly through the clip, made as its thumbnail is, for skimming its card."""
    recipe = _neutral(clip, first_16mib)
    folder = cache_dir() / "skims"
    quad_bayer_remosaic = remosaic_enabled()
    path = folder / f"{clip_id}-{fingerprint(recipe, quad_bayer_remosaic, clip)}.json"
    if (cached := _cached(path)) is not None:
        return [base64.b64decode(frame) for frame in json.loads(cached)]
    with _skimming:
        if (cached := _cached(path)) is not None:  # made by another request meanwhile
            return [base64.b64decode(frame) for frame in json.loads(cached)]
        frames = _render_skim(clip, recipe, quad_bayer_remosaic)
        _store(path, json.dumps([base64.b64encode(frame).decode() for frame in frames]).encode())
        _trim(folder, cap_bytes, keep=path)
    return frames
