"""Export a clip as an OpenEXR folder: a scene-linear half-float EXR per frame and the aligned audio as WAV.

Frames are rendered as the other outputs are (development, exposure, noise reduction), without the look,
its effects or stabilisation, and written in the workers under a temporary name; the parent renames each
into place in order, so after an interruption the frames already named are whole and are kept.
"""

from __future__ import annotations

import contextlib
import dataclasses
import shutil
import uuid
from collections.abc import Callable
from fractions import Fraction
from functools import partial
from pathlib import Path

import numpy as np
import numpy.typing as npt
import OpenEXR

from mcrawsome.develop.deflicker import deflicker_stops, measure_gains
from mcrawsome.io.timecode import export_timecode, from_frame, timecode_note
from mcrawsome.output import chunks
from mcrawsome.output.audio import aligned_track, write_wav
from mcrawsome.output.chunks import Encoder, Manifest, WorkFolder
from mcrawsome.output.export import (
    FULL_DISK,
    ExportError,
    ExportResult,
    _prepare,
    base_measurements,
    failing_as,
    filled_frames,
    finish_interrupted_publish,
    ignored_settings,
    is_disk_full,
    picture_key,
    read_source,
    source_job,
    stops,
    thermal_guard,
    trim_facts,
)
from mcrawsome.output.exr import EXR_SPACES, set_timecode, write_exr
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.output.sidecar import sidecar_path, write_sidecar
from mcrawsome.pipeline import render
from mcrawsome.pipeline.render import iter_frames
from mcrawsome.progress import Progress
from mcrawsome.recipe.schema import Recipe

_OURS = (".exr", ".wav")
_PART = ".part-"  # a frame being written: `.part-<random>.exr`, renamed into place when whole


def _frame_name(clip_path: Path, index: int) -> str:
    return f"{clip_path.stem}_{index:06d}.exr"


def _write_part(
    folder: str, space: str, compression: str, rate: Fraction | None, frame: npt.NDArray[np.float32]
) -> str:
    """Write one frame in a worker under a temporary name, which the parent renames into place (and, with
    time-of-day timecode, gives the frame's own time address)."""
    name = f"{_PART}{uuid.uuid4().hex}.exr"
    write_exr(Path(folder) / name, frame, space=space, compression=compression, rate=rate)
    return name


def read_exr_size(path: Path) -> tuple[int, int]:
    """(width, height) of an EXR's data window."""
    with OpenEXR.File(str(path), header_only=True) as exr:
        (left, top), (right, bottom) = exr.header()["dataWindow"]
    return int(right - left + 1), int(bottom - top + 1)


def _check_folder(folder: Path, force: bool) -> None:
    if sidecar_path(folder).exists() and not force:
        raise ExportError(
            f"{sidecar_path(folder)} already exists (use --force to replace it)", code="output-exists"
        )
    if not folder.exists():
        return
    if not force:
        raise ExportError(f"{folder} already exists (use --force to replace it)", code="output-exists")
    if not folder.is_dir():
        raise ExportError(f"{folder} is a file, not an EXR folder", code="output-exists")
    foreign = [p.name for p in folder.iterdir() if p.suffix.lower() not in _OURS]
    if foreign:
        raise ExportError(
            f"{folder} holds other files ({', '.join(sorted(foreign)[:3])}); not replacing it",
            code="output-exists",
        )


def _verify(stage: Path, clip_path: Path, frames: range, size: tuple[int, int]) -> None:
    count = sum(p.name.startswith(f"{clip_path.stem}_") for p in stage.glob("*.exr"))
    if count != len(frames):
        raise ExportError(f"{count} EXR files written, expected {len(frames)}", code="verify-failed")
    for index in (frames[0], frames[-1]):
        path = stage / _frame_name(clip_path, index)
        try:
            found = read_exr_size(path)
        except Exception as exc:
            raise ExportError(f"{path.name} cannot be read ({exc})", code="verify-failed") from None
        if found != size:
            raise ExportError(f"{path.name} does not have the expected size", code="verify-failed")


@chunks.locked_output
def export_exr(
    clip_path: Path,
    folder: Path,
    recipe: Recipe,
    *,
    force: bool = False,
    workers: int = 4,
    progress: Progress | None = None,
    frame_range: tuple[int, int] | None = None,
    stop: Callable[[], bool] | None = None,
    restart: bool = False,
    thermal_limit: float | None = None,
) -> ExportResult:
    """Frames are written into the work folder, and the finished folder is renamed into place. After an
    interruption, running again keeps the frames already written; `stop()` is asked after each frame. With a
    `thermal_limit` (degrees), rendering waits whenever the CPU is that hot."""
    clip_path, folder = clip_path.absolute(), folder.absolute()
    publishing = not force and WorkFolder(folder).publish_interrupted(folder)
    if not publishing:
        _check_folder(folder, force)
    ffmpeg = find_ffmpeg()[0].absolute()
    facts, video = read_source(clip_path, recipe)
    video_info = video[0] if video is not None else None
    job = source_job(clip_path, recipe, facts, video)
    whole_job, whole_frames = job, facts.frame_count
    first = 0
    if frame_range is not None:
        first, count = frame_range
        with failing_as(clip_path, "trimming the clip", "input-unreadable"):
            facts = trim_facts(clip_path, facts, first, count)
        job = dataclasses.replace(job, first_frame=first)
    folder.parent.mkdir(parents=True, exist_ok=True)
    work = WorkFolder(folder)
    output = recipe.output
    encoder = Encoder(name="openexr", codec="exr", args=[output.exr_space, output.exr_compression])
    wanted = chunks.request(clip_path, recipe, frame_range, encoder, picture_key(job))
    if publishing:
        if (
            finished := finish_interrupted_publish(folder, lambda m: m.model_copy(update=wanted))
        ) is not None:
            return finished
        _check_folder(folder, force)
    stored, notes = chunks.resume_or_restart(work, restart, lambda m: m.model_copy(update=wanted))
    render_workers, budget_note = render.worker_budget(
        job, (facts.width, facts.height), workers, render.available_memory()
    )
    if stored is None:
        resolved, measurements, found = _prepare(
            clip_path, recipe, whole_job, whole_frames, video_info, ffmpeg, frame_range,
            workers, render_workers, work.path, progress,
        )  # fmt: skip
        gains = None
        if resolved.develop.deflicker > 0 and video_info is None:
            if progress:
                progress("measuring", 0, 0)
            with failing_as(clip_path, "measuring the exposure of each frame", "measure-failed"):
                gains = measure_gains(
                    clip_path, resolved.develop.deflicker, resolved.develop.deflicker_seconds, workers,
                    black_level=resolved.develop.black_level, white_level=resolved.develop.white_level,
                    crop=job.crop, quad_bayer_remosaic=job.quad_bayer_remosaic,
                )  # fmt: skip
            measurements["deflicker_max_stops"] = deflicker_stops(gains)
        manifest = Manifest(
            **wanted, resolved=resolved, measurements=measurements, notes=found, frame_gains=gains
        )
        work.save(manifest)
    else:
        manifest = stored
    recipe = manifest.resolved
    job = dataclasses.replace(
        job,
        exposure_stops=stops(recipe),
        frame_gains=tuple(manifest.frame_gains) if manifest.frame_gains is not None else None,
    )
    stage = work.publish_path
    stage.mkdir(exist_ok=True)
    for part in stage.glob(f"{_PART}*"):  # frames an interruption left half written
        part.unlink()
    frames = range(first, first + facts.frame_count)
    done = next(
        (n for n, i in enumerate(frames) if not (stage / _frame_name(clip_path, i)).exists()), len(frames)
    )
    for index in frames[done:]:  # named in order, so nothing after the first missing frame is kept
        (stage / _frame_name(clip_path, index)).unlink(missing_ok=True)
    if stored is not None:
        notes.append(f"resumed: {done} of {len(frames)} frames were already written")
    notes += manifest.notes
    frames_record, filled_note = filled_frames(facts, first)
    if filled_note and video_info is None:
        notes.append(filled_note)
    notes += ignored_settings(recipe)
    if budget_note:
        notes.append(budget_note)
    guard = thermal_guard(thermal_limit)
    try:
        if progress:
            progress("rendering", done, len(frames))
        code = export_timecode(clip_path, output.timecode, output.timecode_zone, float(facts.rate))
        if output.timecode == "time-of-day":
            notes.append(timecode_note(code, first))
        if done < len(frames):
            rate = Fraction(facts.rate) if code is not None else None
            write = partial(_write_part, str(stage), output.exr_space, output.exr_compression, rate)
            emit = (done, len(frames) - done)
            previous: tuple[str, Path] | None = None  # a repeated frame comes back with the same part
            with contextlib.closing(
                iter_frames(job, len(frames), render_workers, emit=emit, finish=write, guard=guard)
            ) as written:
                for part in written:
                    target = stage / _frame_name(clip_path, frames[done])
                    if previous is not None and previous[0] == part:
                        shutil.copyfile(previous[1], target)
                    else:
                        (stage / part).replace(target)
                    if code is not None:
                        set_timecode(target, code.address(frames[done]))
                    previous = (part, target)
                    done += 1
                    if progress:
                        progress("rendering", done, len(frames))
                    if stop is not None and done < len(frames) and stop():
                        raise ExportError(
                            f"{clip_path.name}: paused after {done} of {len(frames)} frames", code="paused"
                        )
        if facts.track is not None:
            seconds = facts.frame_count / float(facts.rate)
            track = aligned_track(facts.track, facts.first_frame_ns, seconds, facts.tempo)  # as the video mux
            write_wav(stage / f"{clip_path.stem}.wav", track, from_frame(code, first) if code else None)
        if progress:
            progress("verifying", 0, 0)
        _verify(stage, clip_path, frames, (facts.width, facts.height))
        measurements = base_measurements(job, facts, 0.0 if facts.track is not None else None)
        measurements |= manifest.measurements
        if video_info is not None:
            measurements["input_encoding"] = job.input_encoding
        measurements |= {
            "exr_space": output.exr_space,
            "exr_compression": output.exr_compression,
            "note": (
                f"scene linear, {EXR_SPACES[output.exr_space].name}, half float, after exposure; the look, "
                "its effects and stabilisation are not applied"
            ),
        }
        with failing_as(clip_path, "writing the sidecar", "verify-failed"):
            staged_sidecar = write_sidecar(
                stage, recipe, measurements, [clip_path], frames_record if video_info is None else None
            )
        if folder.exists():
            shutil.rmtree(folder)
        stage.rename(folder)
        sidecar = sidecar_path(folder)
        staged_sidecar.replace(sidecar)
    except ExportError as exc:
        if is_disk_full(exc):
            raise ExportError(f"{clip_path.name}: {FULL_DISK}", code="disk-full") from None
        raise
    except Exception as exc:  # a worker failed or the disk filled: report it for this clip
        if is_disk_full(exc):
            raise ExportError(f"{clip_path.name}: {FULL_DISK}", code="disk-full") from None
        raise ExportError(f"{clip_path.name}: EXR export failed ({exc})", code="render-failed") from None
    finally:
        if guard is not None:
            notes += guard.notes()
    work.discard()
    return ExportResult(folder, sidecar, facts.frame_count, None, notes)
