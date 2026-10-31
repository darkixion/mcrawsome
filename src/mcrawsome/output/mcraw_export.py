"""Copy compressed RAW frames into a trimmed or repaired, finalised MCRAW container."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import numpy as np

from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import is_video_input
from mcrawsome.io.gcsv import GcsvError, read_gcsv, write_gcsv_slice
from mcrawsome.io.trim import trim
from mcrawsome.motion.sync import raw_frame_times
from mcrawsome.output import chunks
from mcrawsome.output.chunks import Encoder, Manifest, WorkFolder
from mcrawsome.output.export import (
    FULL_DISK,
    ExportError,
    ExportResult,
    check_targets,
    failing_as,
    finish_interrupted_publish,
    is_disk_full,
    thermal_guard,
)
from mcrawsome.output.sidecar import sidecar_path, write_sidecar
from mcrawsome.progress import Progress
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.schema import Recipe

IGNORED = "MCRAW copies the recorded RAW data; development and stabilisation do not apply"
GYRO_LEFT_OUT = "the gyro log cannot be used ({reason}), so it is left out of the output"


def _processing_set(recipe: Recipe) -> bool:
    """True when anything other than the output section differs from the defaults."""
    plain = build_recipe([{"output": {"kind": "mcraw"}}])
    return recipe.model_dump(exclude={"output"}) != plain.model_dump(exclude={"output"})


def _own_leftover_gyro(output: Path, clip_path: Path, gyro_output: Path) -> bool:
    """An earlier run moved its gyro log into place and then stopped before the output: its staged
    sidecar is still in the work folder, its staged gyro log is gone, and the clip had a log to move."""
    staged_gyro = WorkFolder(output).publish_path.with_suffix(".gcsv")
    return (
        gyro_output.exists()
        and clip_path.with_suffix(".gcsv").exists()
        and sidecar_path(WorkFolder(output).publish_path).exists()
        and not staged_gyro.exists()
    )


@chunks.locked_output
def export_mcraw(
    clip_path: Path,
    output: Path,
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
    """Copy without decoding, verify the new index, then rename the staged files into place.
    An interrupted copy is started again, since no frame needs rendering or compression."""
    clip_path, output = clip_path.absolute(), output.absolute()
    if is_video_input(clip_path):
        raise ExportError(f"{clip_path.name}: MCRAW needs a RAW clip", code="recipe-invalid")
    if output.resolve() == clip_path.resolve():
        raise ExportError(f"{output}: cannot replace the input clip", code="output-exists")
    publishing = not force and WorkFolder(output).publish_interrupted(output)
    gyro_output = output.with_suffix(".gcsv")
    leftover = not force and _own_leftover_gyro(output, clip_path, gyro_output)
    if not publishing:
        check_targets(output, force)
    if gyro_output.exists() and not force and not leftover:
        raise ExportError(f"{gyro_output} already exists (use --force to replace it)", code="output-exists")
    if not clip_path.exists():
        raise ExportError(f"{clip_path} does not exist", code="input-missing")
    with failing_as(clip_path, "reading the clip", "input-unreadable"), Clip(clip_path) as clip:
        first, count = frame_range if frame_range is not None else (0, clip.frame_count)
        if first < 0 or count < 1 or first + count > clip.frame_count:
            raise ExportError(f"{clip_path.name}: invalid frame range", code="recipe-invalid")
        timestamps = clip.timestamps_ns
        expected = (count, int(timestamps[first]), int(timestamps[first + count - 1]))
        recovered = clip.recovered
        notes = [IGNORED] if _processing_set(recipe) else []
        if clip.recovery_note:
            notes.append(clip.recovery_note)
        gyro_path = clip.gyro_log_path()
        gyro_range: tuple[float, float] | None = None
        if gyro_path is not None:
            try:
                log = read_gcsv(gyro_path)
                if frame_range is not None:
                    received = np.array([clip.frame_metadata(i).received_ms for i in range(clip.frame_count)])
                    times = raw_frame_times(timestamps, received, log)
                    period = float(np.median(np.diff(times))) if len(times) > 1 else 1 / clip.fps
                    end = (
                        float(times[first + count])
                        if first + count < len(times)
                        else float(times[-1]) + period
                    )
                    gyro_range = (float(times[first]), end)
            except (GcsvError, ValueError, KeyError) as exc:
                notes.append(GYRO_LEFT_OUT.format(reason=exc))
                gyro_path = None
    output.parent.mkdir(parents=True, exist_ok=True)
    work = WorkFolder(output)
    wanted = chunks.request(clip_path, recipe, frame_range, Encoder(name="copy", codec="mcraw", args=[]))
    if publishing:
        if (
            finished := finish_interrupted_publish(output, lambda m: m.model_copy(update=wanted))
        ) is not None:
            return finished
        check_targets(output, force)
    interrupted = work.path.exists() and not restart
    chunks.resume_or_restart(work, True, lambda m: None)
    if interrupted:
        notes.append("interrupted MCRAW copy; starting again")
    measurements: dict[str, object] = {
        "first_frame": first,
        "frame_count": count,
        "first_timestamp_ns": expected[1],
        "last_timestamp_ns": expected[2],
        "recovered": recovered,
        "frames": count,
    }
    if IGNORED in notes:
        measurements["note"] = IGNORED
    work.save(Manifest(**wanted, resolved=recipe, measurements=measurements, notes=notes))
    guard = thermal_guard(thermal_limit)

    def copied(done: int, total: int) -> None:
        if progress:
            progress("rendering", done, total)
        if stop is not None and stop():
            raise ExportError(f"{clip_path.name}: paused after {done} of {total} frames", code="paused")
        if guard is not None:
            guard.wait()

    stage = work.publish_path
    try:
        if progress:
            progress("rendering", 0, count)
        audio_count = trim(
            clip_path, stage, first, count if frame_range is not None else None, on_frame=copied
        )
        staged_gyro = stage.with_suffix(".gcsv")
        if gyro_path is not None:
            if gyro_range is None:
                shutil.copyfile(gyro_path, staged_gyro)
            else:
                write_gcsv_slice(gyro_path, staged_gyro, *gyro_range)
        if progress:
            progress("verifying", 0, 0)
        _verify(stage, expected, audio_count)
        measurements["audio_chunks"] = audio_count
        staged_sidecar = write_sidecar(stage, recipe, measurements, [clip_path])
        if staged_gyro.exists():
            staged_gyro.replace(gyro_output)
        elif force or leftover:
            gyro_output.unlink(missing_ok=True)
        stage.replace(output)  # the sidecar last: a rerun then finds the output and finishes the move
        staged_sidecar.replace(sidecar_path(output))
    except ExportError:
        raise
    except Exception as exc:
        if is_disk_full(exc):
            raise ExportError(f"{clip_path.name}: {FULL_DISK}", code="disk-full") from None
        raise ExportError(f"{clip_path.name}: MCRAW export failed ({exc})", code="render-failed") from None
    work.discard()
    if guard is not None:
        notes += guard.notes()
    return ExportResult(output, sidecar_path(output), count, None, notes)


def _verify(stage: Path, expected: tuple[int, int, int], audio_count: int) -> None:
    with failing_as(stage, "verifying the MCRAW copy", "verify-failed"), Clip(stage) as clip:
        timestamps = clip.timestamps_ns
        actual = (clip.frame_count, int(timestamps[0]), int(timestamps[-1]))
        if clip.recovered or actual != expected or clip.audio_chunk_count != audio_count:
            raise ExportError(
                "MCRAW copy does not have the expected frames, timestamps and audio", code="verify-failed"
            )
