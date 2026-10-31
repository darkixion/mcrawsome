"""Plan and validate synchronous RAW exports before processing begins."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcrawsome.colour.white_balance import validate_clip_white_balance
from mcrawsome.errors import McError, error_code
from mcrawsome.io.container import ContainerError
from mcrawsome.io.directlog import UNKNOWN_COLOUR, DirectLogError, is_video_input, probe_direct_log
from mcrawsome.output.chunks import WorkFolder
from mcrawsome.output.export import ExportError, ignored_settings, input_encoding_for, trimmed_range
from mcrawsome.output.ffmpeg import FfmpegError, find_ffmpeg
from mcrawsome.output.sidecar import sidecar_path
from mcrawsome.output.space import check_batch
from mcrawsome.recipe.load import RecipeError, build_recipe, default_layer_paths, load_toml, parse_override
from mcrawsome.recipe.schema import Recipe

log = logging.getLogger(__name__)

SUFFIXES: dict[str, tuple[str, ...]] = {"dng": (), "exr": (), "mcraw": (".mcraw",)}
KINDS = ("dng", "exr", "mcraw")


def _failure(exc: BaseException, message: str | None = None) -> McError:
    return McError(message if message is not None else str(exc), code=error_code(exc))


def resolve_recipe(
    clip: Path,
    recipes: Sequence[Path | dict[str, Any]],
    to: str | None,
    fields: dict[str, Any],
    sets: tuple[str, ...],
    motion: dict[str, Any] | None = None,
    *,
    saved: bool = True,
) -> Recipe:
    """The clip's Recipe: its saved layers (unless not `saved`), then the given ones. A Direct Log video
    exports as OpenEXR unless a layer says otherwise, since CinemaDNG needs RAW frames."""
    layers: list[dict[str, Any]] = [{"output": {"kind": "exr"}}] if is_video_input(clip) else []
    layers += [load_toml(p) for p in default_layer_paths(clip)] if saved else []
    layers += [r if isinstance(r, dict) else load_toml(r) for r in recipes]  # files, or the API's tables
    if to:
        layers.append({"output": {"kind": to}})
    if motion:
        layers.append({"motion": motion})
    for key, value in fields.items():
        if value is not None:
            layers.append(parse_override(f"{key.replace('__', '.')}={value}"))
    layers += [parse_override(s) for s in sets]
    recipe = build_recipe(layers)
    validate_clip_white_balance(clip, recipe)
    return recipe


def output_name(clip: Path, recipe: Recipe, frame_range: tuple[int, int] | None = None) -> str:
    kind = recipe.output.kind
    if kind == "mcraw":
        if frame_range is not None:
            first, count = frame_range
            return f"{clip.stem}_{first}-{first + count - 1}.mcraw"
        return f"{clip.stem}_repaired.mcraw"
    if kind == "dng":
        return f"{clip.stem}_DNG{'_half' if recipe.output.dng_scale == 'half' else ''}"
    return f"{clip.stem}_EXR"


def _target(
    clip: Path, recipe: Recipe, output: str | None, several: bool, frame_range: tuple[int, int] | None = None
) -> Path:
    """Where a clip's export goes. A folder is meant when --output is an existing folder, ends with a
    slash, has no file extension, or several clips are exported. A DNG export is always a folder,
    created inside the --output folder."""
    name = output_name(clip, recipe, frame_range)
    if output is None:
        return clip.with_name(name)
    path = Path(output)
    allowed = SUFFIXES[recipe.output.kind]
    if several or output.endswith(("/", os.sep)) or path.is_dir() or not path.suffix:
        if path.exists() and not path.is_dir():
            raise McError(f"{path} is a file, but --output must be a folder here", code="output-exists")
        return path / name
    if path.suffix.lower() not in allowed:
        what = (
            f"use {', '.join(allowed)} or a folder" if allowed else "give a folder to put the DNG folder in"
        )
        raise McError(
            f"{path.name}: unsupported output for --to {recipe.output.kind}; {what}", code="recipe-invalid"
        )
    return path


def _check_video(video: Path, recipe: Recipe) -> None:
    """Find a Direct Log video's problems while the batch is planned, not when its turn comes."""
    try:
        info = probe_direct_log(find_ffmpeg()[1], video)
    except (DirectLogError, FfmpegError) as exc:
        raise _failure(exc) from None
    if input_encoding_for(recipe, info.encoding) is None:
        raise McError(f"{video.name}: {UNKNOWN_COLOUR}", code="input-unreadable")


@dataclass
class PlannedExport:
    """One clip of a planned batch. `notes` are about this clip (a range cut short), as "<clip>: note",
    and `ignored` the settings that do nothing for this clip (shown by a dry run; the export notes them)."""

    clip: Path
    recipe: Recipe
    target: Path
    frame_range: tuple[int, int] | None
    notes: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def plan_export(
    clips: tuple[Path, ...] | list[Path],
    recipes: Sequence[Path | dict[str, Any]],
    to: str | None,
    stabilise: str | None,
    crop: float | None,
    start: str | None,
    duration: str | None,
    fields: dict[str, Any],
    sets: tuple[str, ...],
    output: str | None,
    force: bool,
    dry_run: bool,
    space_check: bool = True,
    *,
    beside: str | None = None,
    saved: bool = True,
) -> list[PlannedExport]:
    """Plan every clip of the batch, or raise McError for the first problem. A dry run allows missing
    clips and existing outputs, and checks no tools or disk space. `beside` supplies a default
    subfolder beside each clip when no output is given; without `saved` the clips' saved Recipes are
    not read."""
    plan: list[PlannedExport] = []
    seen: dict[Path, Path] = {}
    missing = [clip for clip in clips if not clip.exists()]
    for clip in clips:
        try:
            motion = {k: v for k, v in (("stabilise", stabilise), ("crop", crop)) if v is not None}
            recipe = resolve_recipe(clip, recipes, to, fields, sets, motion, saved=saved)
        except RecipeError as exc:
            raise _failure(exc, f"{clip.name}: {exc}") from None
        if recipe.output.kind == "mcraw" and is_video_input(clip):
            raise McError(f"{clip.name}: MCRAW needs a RAW clip", code="recipe-invalid")
        if recipe.output.kind == "dng" and is_video_input(clip):
            raise McError(
                f"{clip.name}: CinemaDNG needs a RAW (.mcraw) clip; this is a video", code="recipe-invalid"
            )
        if is_video_input(clip) and clip not in missing:
            _check_video(clip, recipe)
        frame_range, notes = None, []
        if clip not in missing:
            try:
                # MCRAW output copies the recorded frames, so its ranges count them
                copy = recipe.output.kind == "mcraw"
                fill = recipe.output.fill_dropped and not copy
                rate = "keep" if copy else recipe.output.frame_rate
                frame_range, note = trimmed_range(clip, recipe, start, duration, fill, rate)
            except (ExportError, ValueError, ContainerError, DirectLogError) as exc:
                raise _failure(exc) from None
            if note:
                notes.append(f"{clip.name}: {note}")
        destination = output
        if destination is None and beside is not None:
            destination = str(clip.parent / beside) + os.sep
        target = _target(clip, recipe, destination, several=len(clips) > 1, frame_range=frame_range)
        if target.resolve() == clip.resolve():
            raise McError(f"{target}: cannot replace the input clip", code="output-exists")
        if target.resolve() in seen:
            raise McError(
                f"{seen[target.resolve()].name} and {clip.name} would write the same output {target}",
                code="output-exists",
            )
        seen[target.resolve()] = clip
        existing = next((p for p in (target, sidecar_path(target)) if p.exists()), None)
        if (
            existing is not None
            and not force
            and not dry_run
            and not WorkFolder(target).publish_interrupted(target)
        ):
            raise McError(f"{existing} already exists (use --force to replace it)", code="output-exists")
        plan.append(PlannedExport(clip, recipe, target, frame_range, notes, ignored_settings(recipe)))
    if not dry_run and space_check:
        check_batch([(p.clip, p.recipe, p.target, p.frame_range) for p in plan])
    return plan
