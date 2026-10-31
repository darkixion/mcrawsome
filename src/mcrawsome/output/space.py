"""How much disk an export needs, and a check that the folder has it before anything is rendered."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerError
from mcrawsome.io.directlog import is_video_input
from mcrawsome.output.dng import SCALE_FACTORS
from mcrawsome.output.export import ExportError, read_clip_facts, read_video_facts
from mcrawsome.output.ffmpeg import FfmpegError, find_ffmpeg
from mcrawsome.recipe.schema import Recipe

_AUDIO_BPS = 1_536_000  # 48 kHz 16-bit stereo PCM, written beside DNG and EXR frames
_LOSSLESS_JPEG = 3  # CinemaDNG with lossless JPEG is about a third of the uncompressed size
_COPIES = 1.1  # a folder is written once, in place, with a little room for the audio and last frames


def estimate_bytes(recipe: Recipe, width: int, height: int, frames: int, fps: float) -> int:
    """About how large the finished CinemaDNG or OpenEXR output is."""
    audio = frames / fps * _AUDIO_BPS / 8
    if recipe.output.kind == "dng":  # 16-bit samples, no compression; the sensor size is the frame size
        factor = SCALE_FACTORS[recipe.output.dng_scale]
        if factor > 1:
            width, height = width // (2 * factor) * 2, height // (2 * factor) * 2
        picture = (
            width
            * height
            * 2
            * frames
            / (_LOSSLESS_JPEG if recipe.output.dng_compression == "lossless" else 1)
        )
        return int(picture + audio)
    return int(width * height * 6 * frames + audio)  # half-float R, G and B; compression only shrinks it


def human_size(size: float) -> str:
    """Decimal units, as disk makers and file managers count them."""
    units = ["bytes", "KB", "MB", "GB", "TB"]
    unit = units.pop(0)
    while size >= 1000 and units:
        size /= 1000
        unit = units.pop(0)
    return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"


def _existing(folder: Path) -> Path:
    folder = folder.resolve()
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return folder


def check_space(folder: Path, needed: int, copies: float = _COPIES) -> None:
    """Raise McError (disk-full) unless `folder` (or the nearest folder that exists) has `copies` times the
    estimated output free."""
    free = shutil.disk_usage(_existing(folder)).free
    required = int(needed * copies)
    if free < required:
        raise McError(
            f"{folder}: {human_size(free)} free, but about {human_size(required)} is needed "
            f"({human_size(needed)} of output, and a little room for the audio and the last frames); "
            "free some space, choose another folder with --output, or skip this check with --no-space-check",
            code="disk-full",
        )


def estimate_clip(clip: Path, recipe: Recipe, frame_range: tuple[int, int] | None) -> int | None:
    """The estimate for a clip, or None when the clip cannot be read (its export reports why)."""
    try:
        if recipe.output.kind == "mcraw":
            with Clip(clip) as source:
                fraction = frame_range[1] / source.frame_count if frame_range else 1.0
            return int(clip.stat().st_size * fraction * 1.1)
        if is_video_input(clip):
            facts = read_video_facts(find_ffmpeg()[0], clip, recipe)[0]
        else:
            facts = read_clip_facts(clip, recipe)
    except (ExportError, FfmpegError, ContainerError, OSError, ValueError):
        return None
    frames = frame_range[1] if frame_range is not None else facts.frame_count
    return estimate_bytes(recipe, facts.width, facts.height, frames, facts.measured_fps)


@dataclass
class SpaceRequirement:
    folder: Path
    free: int
    total: int = 0
    copies: float = 0.0
    folders: dict[Path, int] = field(default_factory=dict)  # output bytes wanted in each folder

    @property
    def required(self) -> int:
        return int(self.total * self.copies)


def space_requirements(outputs: list[tuple[Path, int]]) -> list[SpaceRequirement]:
    """Sum output sizes per filesystem, with the working-space margin."""
    disks: dict[int, SpaceRequirement] = {}
    for output, size in outputs:
        existing = _existing(output.parent)
        device = existing.stat().st_dev
        if device not in disks:
            disks[device] = SpaceRequirement(output.parent, shutil.disk_usage(existing)[2])
        disk = disks[device]
        disk.total += size
        disk.folders[output.parent] = disk.folders.get(output.parent, 0) + size
        disk.copies = _COPIES
    return list(disks.values())


def check_batch(items: list[tuple[Path, Recipe, Path, tuple[int, int] | None]]) -> None:
    """Check the combined space of outputs sharing each filesystem."""
    outputs: list[tuple[Path, int]] = []
    for clip, recipe, output, frame_range in items:
        size = estimate_clip(clip, recipe, frame_range)
        if size is not None:
            outputs.append((output, size))
    for disk in space_requirements(outputs):
        if len(disk.folders) == 1:
            check_space(disk.folder, disk.total, disk.copies)
        elif disk.free < disk.required:
            each = "; ".join(
                f"{folder} needs {human_size(int(size * disk.copies))}"
                for folder, size in disk.folders.items()
            )
            raise McError(
                f"{len(disk.folders)} output folders share one disk with {human_size(disk.free)} free, "
                f"but about {human_size(disk.required)} is needed in all ({each}); "
                "free some space, choose another folder with --output, "
                "or skip this check with --no-space-check",
                code="disk-full",
            )
