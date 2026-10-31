"""Export a clip as a CinemaDNG folder: one raw DNG per frame plus the aligned audio as WAV.

No development happens: full-size DNGs hold the sensor values as recorded, while half-size proxies average
four samples per Bayer site. Both keep the camera's colour data and lens shading for raw developers.
"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import math
import shutil
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from functools import partial
from pathlib import Path
from typing import Literal

from mcrawsome.analysis.resolve import resolve_auto
from mcrawsome.camera.shading import ShadingMode
from mcrawsome.camera.vignette import VignetteProfile, apply_profile
from mcrawsome.develop.crop import CropBox, crop_raw
from mcrawsome.develop.deflicker import deflicker_stops, measure_gains
from mcrawsome.develop.raw import levels
from mcrawsome.io.clip import CfaLayout, CfaPattern, Clip, RawFrame, close_all, remosaic_enabled
from mcrawsome.io.directlog import is_video_input
from mcrawsome.io.timecode import Timecode, export_timecode, from_frame, timecode_note
from mcrawsome.output import chunks, dng_log
from mcrawsome.output.audio import aligned_track, write_wav
from mcrawsome.output.chunks import Encoder, Manifest, WorkFolder
from mcrawsome.output.dng import (
    SCALE_FACTORS,
    TILE,
    DngScale,
    ShadingBaker,
    bake_shading,
    baked_white,
    camera_model,
    dng_info,
    exposure_headroom,
    read_dng_tags,
    shading_scale_bits,
    with_camera_model,
    with_matrix,
    write_dng,
)
from mcrawsome.output.export import (
    FULL_DISK,
    ExportError,
    ExportResult,
    base_measurements,
    failing_as,
    filled_frames,
    finish_interrupted_publish,
    frame_job,
    ignored_settings,
    is_disk_full,
    picture_key,
    read_clip_facts,
    stops,
    thermal_guard,
    trim_facts,
)
from mcrawsome.output.sidecar import sidecar_path, write_sidecar
from mcrawsome.pipeline.pool import bounded_imap, worker_pool
from mcrawsome.pipeline.render import FrameJob
from mcrawsome.progress import Progress
from mcrawsome.recipe.schema import Recipe

_OURS = (".dng", ".wav")
_SAMPLES = {2: "four", 4: "16", 8: "64"}
_clips: dict[Path, Clip] = {}


@atexit.register
def _close_clips() -> None:
    """Close the clips kept open for the process (a worker's, or the caller's when run in-process)."""
    clips = list(_clips.values())
    _clips.clear()
    close_all(clips)


@dataclass(frozen=True)
class DngSettings:
    """What every frame of one DNG export is written with."""

    exposure: float
    lens_shading: float
    compression: Literal["none", "lossless"]
    neutral: tuple[float, float, float]  # the Recipe's white balance, so an editor shows one white balance
    scale: DngScale = "full"
    black_level: float | tuple[float, float, float, float] | None = None
    white_level: float | None = None
    shading_mode: ShadingMode = "full"
    baked_bits: int | None = None  # the lens shading is in the values, scaled by 2^bits (see bake_shading)
    log_bits: int | None = None  # log-encoded values of this many bits (see dng_log)
    names: tuple[str, str, str] | None = None  # Make, Model and UniqueCameraModel replacing the defaults
    colour_matrix: tuple[float, ...] | None = None  # replaces the colour matrices (see with_matrix)
    crop: CropBox | None = None  # the part of the raw frame written (develop.crop)
    vignette_profile: VignetteProfile | None = None
    frame_gains: tuple[float, ...] | None = None  # deflicker: each recorded frame's gain, as BaselineExposure
    exposure_bits: int | None = (
        None  # deflicker baked into the values with this headroom (output.dng_deflicker)
    )
    rotation: int = 0
    quad_bayer_remosaic: bool = dataclasses.field(default_factory=remosaic_enabled)
    cfa_layout: CfaLayout = "auto"
    cfa_pattern: CfaPattern = "recorded"
    timecode: Timecode | None = None  # time-of-day timecode from the whole clip's first frame

    def reading(self) -> tuple[bool, str, str]:
        return self.quad_bayer_remosaic, self.cfa_layout, self.cfa_pattern


def _write_frame(
    clip_path: Path, folder: Path, rate: Fraction, settings: DngSettings, frame: tuple[int, int]
) -> int:
    """`frame` is (output frame, source frame): a repeat is written again under the next number."""
    index, source = frame
    if clip_path in _clips:
        open_as = _clips[clip_path]
        if (open_as.quad_bayer_remosaic, open_as.cfa_layout, open_as.cfa_pattern) != settings.reading():
            _clips.pop(clip_path).close()
    if clip_path not in _clips:  # one open clip per worker process
        _clips[clip_path] = Clip(
            clip_path,
            quad_bayer_remosaic=settings.quad_bayer_remosaic,
            cfa_layout=settings.cfa_layout,
            cfa_pattern=settings.cfa_pattern,
        )
    clip = _clips[clip_path]
    raw = clip.raw_frame(source)
    raw = dataclasses.replace(
        raw, metadata=apply_profile(clip.metadata, raw.metadata, settings.vignette_profile)
    )
    if settings.crop is not None:
        raw = crop_raw(raw, settings.crop)
    data = raw.data
    exposure = settings.exposure
    if settings.exposure_bits is not None and settings.frame_gains is not None:
        gain = settings.frame_gains[source]
        return _write_baked_exposure(clip_path, clip, folder, rate, settings, index, raw, gain)
    if settings.frame_gains is not None:
        exposure += math.log2(settings.frame_gains[source])
    info = dng_info(
        clip.metadata, raw.metadata, rate, exposure, settings.lens_shading,
        shading_mode=settings.shading_mode, black_level=settings.black_level,
        white_level=settings.white_level, baked_bits=settings.baked_bits,
        rotation=settings.rotation,
    )  # fmt: skip
    if settings.baked_bits is not None:
        blacks, white = levels(raw.metadata, settings.black_level, settings.white_level)
        data, baked_white = bake_shading(
            data, raw.metadata, info.cfa, settings.lens_shading, settings.shading_mode, blacks, white,
            settings.baked_bits,
        )  # fmt: skip
        info = dataclasses.replace(info, white_level=baked_white)
    info = dataclasses.replace(info, as_shot_neutral=settings.neutral)
    if settings.names is not None:
        info = with_camera_model(info, settings.names)
    if settings.colour_matrix is not None:
        info = with_matrix(info, settings.colour_matrix)
    if settings.timecode is not None:
        info = dataclasses.replace(info, timecodes=settings.timecode.address(index))
    write_dng(
        folder / _frame_name(clip_path, index), data, info, settings.compression, scale=settings.scale,
        log_bits=settings.log_bits, seed=index,
    )  # fmt: skip
    return index


def _write_baked_exposure(
    clip_path: Path,
    clip: Clip,
    folder: Path,
    rate: Fraction,
    settings: DngSettings,
    index: int,
    raw: RawFrame,
    gain: float,
) -> int:
    """A frame with its deflicker gain in the raw values (and its lens shading, when that is baked too), in
    one pass as a picture-normalised mount serves it; the exposure is raised by the headroom alone."""
    shading = settings.baked_bits is not None
    bits = (settings.baked_bits or 0) + (settings.exposure_bits or 0)
    info = dng_info(
        clip.metadata, raw.metadata, rate, settings.exposure, settings.lens_shading,
        shading_mode=settings.shading_mode, black_level=settings.black_level,
        white_level=settings.white_level, baked_bits=bits if shading else None, rotation=settings.rotation,
    )  # fmt: skip
    if not shading:
        info = dataclasses.replace(info, baseline_exposure=info.baseline_exposure + bits)
    baker = _bakers.setdefault(
        (clip.metadata.sensor_arrangement, settings.lens_shading, settings.shading_mode, bits, shading),
        ShadingBaker(clip.metadata.sensor_arrangement, settings.lens_shading, settings.shading_mode, bits,
                     shading=shading),
    )  # fmt: skip
    blacks, white = levels(raw.metadata, settings.black_level, settings.white_level)
    data = baker.bake(raw.data, raw.metadata, blacks, white, gain)
    info = dataclasses.replace(info, white_level=baker.white(blacks, white), as_shot_neutral=settings.neutral)
    if settings.names is not None:
        info = with_camera_model(info, settings.names)
    if settings.colour_matrix is not None:
        info = with_matrix(info, settings.colour_matrix)
    if settings.timecode is not None:
        info = dataclasses.replace(info, timecodes=settings.timecode.address(index))
    write_dng(
        folder / _frame_name(clip_path, index), data, info, settings.compression, scale=settings.scale,
        log_bits=settings.log_bits, seed=index,
    )  # fmt: skip
    return index


_bakers: dict[tuple[object, ...], ShadingBaker] = {}  # one per worker process and settings


def _exposure_bits(
    clip_path: Path, recipe: Recipe, gains: list[float] | None, baked: int | None
) -> int | None:
    """The headroom for a baked deflicker, or None to store it as BaselineExposure: the largest brightening
    rounded up to a power of two (at most 2), less whatever would take the first frame past 16 bits."""
    if recipe.output.dng_deflicker != "baked" or gains is None:
        return None
    with Clip(clip_path) as clip:
        first = clip.frame_metadata(0)
    blacks, white = levels(first, recipe.develop.black_level, recipe.develop.white_level)
    bits = exposure_headroom(gains)
    while bits and baked_white(blacks, white, bits + (baked or 0)) > 65535:
        bits -= 1
    return bits


def _baked_bits(clip_path: Path, recipe: Recipe) -> int | None:
    """None for gain maps, else the scale needed by every frame (0 without Scale Raw).

    Check metadata only, keeping one frame's map at a time. Refuse ranges that cannot fit in 16 bits.
    """
    if recipe.output.dng_shading != "baked":
        return None
    if not recipe.output.dng_scale_raw:
        return 0
    with Clip(clip_path) as clip:
        bits = max(
            shading_scale_bits(
                apply_profile(clip.metadata, clip.frame_metadata(i), recipe.develop.vignette_profile),
                recipe.develop.lens_shading,
                recipe.develop.lens_shading_mode,
            )
            for i in range(clip.frame_count)
        )
        for i in range(clip.frame_count):
            blacks, white = levels(
                clip.frame_metadata(i), recipe.develop.black_level, recipe.develop.white_level
            )
            if round(max(blacks) + (white - max(blacks)) * 2**bits) > 65535:
                raise ExportError(
                    f"{clip_path.name}: baked shading exceeds the 16-bit raw range; "
                    "lower develop.white_level or develop.lens_shading",
                    code="recipe-invalid",
                )
    return bits


def _log_bits(clip_path: Path, recipe: Recipe, baked: int | None) -> int | None:
    """The bits of a log DNG's codes, or None for linear values. Fewer than dng_log.MIN_BITS is refused."""
    output = recipe.output
    if output.dng_transfer != "log":
        return None
    with Clip(clip_path) as clip:
        first = clip.frame_metadata(0)
    _, white = levels(first, recipe.develop.black_level, recipe.develop.white_level)
    sensor = round(white).bit_length()
    bits = dng_log.output_bits(sensor, baked or 0, output.dng_reduce_bits)
    if bits < dng_log.MIN_BITS:
        raise ExportError(
            f"{clip_path.name}: output.dng_reduce_bits {output.dng_reduce_bits} leaves {bits} bits of a "
            f"{sensor}-bit clip; log DNGs need at least {dng_log.MIN_BITS}",
            code="recipe-invalid",
        )
    return min(bits, 16)


def _names(clip_path: Path, recipe: Recipe) -> tuple[str, str, str] | None:
    """The camera names the Recipe asks for, or None to keep the defaults."""
    output = recipe.output
    if output.dng_camera_model is None and not output.dng_unique_model:
        return None
    with Clip(clip_path) as clip:
        device = clip.metadata.device_model or "MotionCam"
    return camera_model(device, clip_path.stem, output.dng_camera_model, output.dng_unique_model)


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
        raise ExportError(f"{folder} is a file, not a DNG folder", code="output-exists")
    foreign = [p.name for p in folder.iterdir() if p.suffix.lower() not in _OURS]
    if foreign:
        raise ExportError(
            f"{folder} holds other files ({', '.join(sorted(foreign)[:3])}); not replacing it",
            code="output-exists",
        )


@chunks.locked_output
def export_dng(
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
    `thermal_limit` (degrees), writing waits whenever the CPU is that hot."""
    clip_path, folder = clip_path.absolute(), folder.absolute()
    if is_video_input(clip_path):
        raise ExportError(
            f"{clip_path.name}: CinemaDNG needs a RAW (.mcraw) clip; this is a video", code="recipe-invalid"
        )
    if recipe.develop.level_policy in ("recorded", "static"):
        with Clip(clip_path) as clip:
            effective = recipe.develop.serving(clip.frame_metadata(0))
        recipe = recipe.model_copy(update={"develop": effective})
    publishing = not force and WorkFolder(folder).publish_interrupted(folder)
    if not publishing:
        _check_folder(folder, force)
    facts = read_clip_facts(clip_path, recipe)
    whole = facts
    first = 0
    if frame_range is not None:
        first, count = frame_range
        with failing_as(clip_path, "trimming the clip", "input-unreadable"):
            facts = trim_facts(clip_path, facts, first, count)
    folder.parent.mkdir(parents=True, exist_ok=True)
    work = WorkFolder(folder)
    encoder = Encoder(name="cinemadng", codec="dng", args=[recipe.output.dng_compression])
    if recipe.output.dng_camera_model == "Panasonic Varicam RAW":
        encoder.args.append("panasonic-dng-exposure-v1")  # invalidate older resumable DNG metadata
    whole_job = frame_job(clip_path, recipe, whole)
    wanted = chunks.request(clip_path, recipe, frame_range, encoder, picture_key(whole_job))
    if publishing:
        if (
            finished := finish_interrupted_publish(folder, lambda m: m.model_copy(update=wanted))
        ) is not None:
            return finished
        _check_folder(folder, force)
    stored, notes = chunks.resume_or_restart(work, restart, lambda m: m.model_copy(update=wanted))
    if stored is None:
        if progress:
            progress("measuring", 0, 0)
        with failing_as(clip_path, "measuring the clip", "measure-failed"):
            resolution = resolve_auto(recipe, whole_job, whole.frame_count, workers, progress=progress)
        measurements = dict(resolution.measurements)
        if frame_range is not None:
            measurements["first_frame"] = first
        found = list(resolution.notes)
        with Clip(
            clip_path,
            quad_bayer_remosaic=whole_job.quad_bayer_remosaic,
            cfa_layout=whole_job.cfa_layout,
            cfa_pattern=whole_job.cfa_pattern,
        ) as clip:
            found += [note for note in (clip.recovery_note, clip.remosaic_note) if note]
        gains = None
        if recipe.develop.deflicker > 0:
            if progress:
                progress("measuring", 0, 0)
            with failing_as(clip_path, "measuring the exposure of each frame", "measure-failed"):
                gains = measure_gains(
                    clip_path, recipe.develop.deflicker, recipe.develop.deflicker_seconds, workers,
                    black_level=recipe.develop.black_level, white_level=recipe.develop.white_level,
                    crop=dataclasses.astuple(facts.crop) if facts.crop is not None else None,
                    quad_bayer_remosaic=whole_job.quad_bayer_remosaic,
                )  # fmt: skip
            measurements["deflicker_max_stops"] = deflicker_stops(gains)
        manifest = Manifest(
            **wanted, resolved=resolution.recipe, measurements=measurements, notes=found, frame_gains=gains
        )
        work.save(manifest)
    else:
        manifest = stored
    recipe = manifest.resolved
    exposure = stops(recipe)
    stage = work.publish_path
    stage.mkdir(exist_ok=True)
    size = (facts.width, facts.height)
    # DNG width/height describe the untouched mosaic; the orientation tag turns its display.
    if recipe.develop.rotation % 180:
        size = size[::-1]
    sensor_width, sensor_height = size
    factor = SCALE_FACTORS[recipe.output.dng_scale]
    if factor > 1:
        block = 2 * factor
        size = (sensor_width // block * 2, sensor_height // block * 2)
        if sensor_width % block or sensor_height % block:
            notes.append(
                f"{recipe.output.dng_scale}-resolution DNG: cropped {sensor_width}x{sensor_height} to "
                f"{size[0] * factor}x{size[1] * factor} before averaging"
            )
    frames = range(first, first + facts.frame_count)
    todo = [i for i in frames if _frame_problem(stage / _frame_name(clip_path, i), size) is not None]
    done = len(frames) - len(todo)
    if stored is not None:
        notes.append(f"resumed: {done} of {len(frames)} frames were already written")
    notes += manifest.notes
    frames_record, filled_note = filled_frames(facts, first)
    if filled_note:
        notes.append(filled_note)
    notes += ignored_settings(recipe)
    try:
        baked = _baked_bits(clip_path, recipe)
        exposure_bits = _exposure_bits(clip_path, recipe, manifest.frame_gains, baked)
        settings = DngSettings(
            quad_bayer_remosaic=whole_job.quad_bayer_remosaic,
            cfa_layout=whole_job.cfa_layout,
            cfa_pattern=whole_job.cfa_pattern,
            exposure=exposure,
            rotation=recipe.develop.rotation,
            lens_shading=recipe.develop.lens_shading,
            compression=recipe.output.dng_compression,
            neutral=facts.neutral,
            scale=recipe.output.dng_scale,
            black_level=recipe.develop.black_level,
            white_level=recipe.develop.white_level,
            shading_mode=recipe.develop.lens_shading_mode,
            vignette_profile=recipe.develop.vignette_profile,
            baked_bits=baked,
            log_bits=_log_bits(clip_path, recipe, (baked or 0) + (exposure_bits or 0)),
            names=_names(clip_path, recipe),
            colour_matrix=recipe.camera.colour_matrix,
            crop=facts.crop,
            frame_gains=tuple(manifest.frame_gains) if manifest.frame_gains is not None else None,
            exposure_bits=exposure_bits,
            timecode=export_timecode(
                clip_path, recipe.output.timecode, recipe.output.timecode_zone, float(facts.rate)
            ),
        )
        if recipe.output.timecode == "time-of-day":
            notes.append(timecode_note(settings.timecode, first))
        write = partial(_write_frame, clip_path, stage, facts.rate, settings)
        if progress:
            progress("rendering", done, len(frames))
        guard = thermal_guard(thermal_limit)

        def cooled() -> Iterator[tuple[int, int]]:
            for index in todo:
                if guard is not None:
                    guard.wait()
                yield index, facts.sources[index] if facts.sources is not None else index

        if todo:
            with (
                worker_pool(workers) as pool,
                contextlib.closing(bounded_imap(pool, write, cooled(), window=workers * 2)) as written,
            ):
                for _ in written:
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
            sound = from_frame(settings.timecode, first) if settings.timecode is not None else None
            write_wav(stage / f"{clip_path.stem}.wav", track, sound)
        if progress:
            progress("verifying", 0, 0)
        _verify(stage, clip_path.stem, facts.frame_count, size)
        job = FrameJob(clip_path=clip_path, exposure_stops=exposure, neutral=facts.neutral)
        measurements = base_measurements(job, facts, 0.0 if facts.track is not None else None)
        measurements |= manifest.measurements
        measurements["note"] = (
            "raw sensor values; exposure is stored as BaselineExposure, the look is not applied"
        )
        if settings.baked_bits is not None:
            measurements["dng_shading_scale_bits"] = settings.baked_bits
        if settings.exposure_bits is not None:
            measurements["dng_exposure_bits"] = settings.exposure_bits
            measurements["note"] = "raw sensor values with the deflicker in them; the look is not applied"
        if settings.log_bits is not None:
            measurements["dng_log_bits"] = settings.log_bits
            measurements["dng_log_curve"] = dng_log.CURVE
        if factor > 1:
            measurements["note"] = (
                f"{recipe.output.dng_scale}-resolution proxy: each Bayer site is the mean of "
                f"{_SAMPLES[factor]} sensor samples; exposure is stored as BaselineExposure, the look is not "
                "applied"
            )
        with failing_as(clip_path, "writing the sidecar", "verify-failed"):
            staged_sidecar = write_sidecar(stage, recipe, measurements, [clip_path], frames_record)
        if folder.exists():
            shutil.rmtree(folder)
        stage.rename(folder)
        sidecar = sidecar_path(folder)
        staged_sidecar.replace(sidecar)
    except ExportError:
        raise
    except Exception as exc:  # a worker failed or the disk filled: report it for this clip
        if is_disk_full(exc):
            raise ExportError(f"{clip_path.name}: {FULL_DISK}", code="disk-full") from None
        raise ExportError(f"{clip_path.name}: DNG export failed ({exc})", code="render-failed") from None
    work.discard()
    if guard is not None:
        notes += guard.notes()

    return ExportResult(folder, sidecar, facts.frame_count, None, notes)


def _frame_name(clip_path: Path, index: int) -> str:
    return f"{clip_path.stem}-{index:06d}.dng"


def _frame_problem(path: Path, size: tuple[int, int]) -> str | None:
    """What is wrong with a written frame, or None when it is whole: the expected size and tiles, and all
    of its image data inside the file (an interrupted write leaves it short)."""
    try:
        tags = read_dng_tags(path)
        if (tags[256][0], tags[257][0]) != size:
            return f"{path.name} does not have the expected size"
        if tags[259][0] == 7:  # lossless JPEG tiles: one per TILE x TILE block, none empty
            offsets, counts = tags[324], tags[325]
            tiles = -(-size[0] // TILE) * -(-size[1] // TILE)
            if len(counts) != tiles or min(counts) == 0:
                return f"{path.name} does not have the expected tiles"
        else:
            offsets, counts = tags[273], tags[279]
            if counts[0] != size[0] * size[1] * tags[258][0] // 8:
                return f"{path.name} does not have the expected size"
        if max(o + c for o, c in zip(offsets, counts, strict=True)) > path.stat().st_size:
            return f"{path.name} is cut short"
    except (OSError, ValueError, KeyError, IndexError, struct.error) as exc:
        return f"{path.name} cannot be read ({exc})"
    return None


def _verify(stage: Path, stem: str, frames: int, size: tuple[int, int]) -> None:
    files = sorted(stage.glob(f"{stem}-*.dng"))
    if len(files) != frames:
        raise ExportError(f"{len(files)} DNG files written, expected {frames}", code="verify-failed")
    for path in (files[0], files[-1]):
        problem = _frame_problem(path, size)
        if problem:
            raise ExportError(problem, code="verify-failed")
