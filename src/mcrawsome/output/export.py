"""What the CinemaDNG, OpenEXR and MCRAW exports share: a clip's facts, its frame job and range, the
measuring and motion step, and the export results."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import itertools
import json
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from mcrawsome.analysis.measure import white_balance_spread
from mcrawsome.analysis.report import sample_advice
from mcrawsome.analysis.resolve import resolve_auto
from mcrawsome.camera.colour import camera_to_ap1, native_to_ap1, with_colour_matrix
from mcrawsome.camera.profile import all_profiles
from mcrawsome.colour.encoding import LOGC3_DEFAULT_EI, ColourEncoding, encoding
from mcrawsome.colour.spaces import CAMERA_NATIVE
from mcrawsome.colour.white_balance import clip_neutral
from mcrawsome.develop.crop import CropBox, CropError, clip_crop
from mcrawsome.develop.raw import levels
from mcrawsome.errors import ErrorCode, McError
from mcrawsome.io.clip import RAW_DECODE_VERSION, AudioTrack, Clip
from mcrawsome.io.container import ContainerError
from mcrawsome.io.describe import RateChoice, frame_map, rate_map, target_rate
from mcrawsome.io.directlog import (
    UNKNOWN_COLOUR,
    DirectLogError,
    DirectLogInfo,
    is_video_input,
    probe_direct_log,
    read_audio,
)
from mcrawsome.io.metadata import FrameMetadata
from mcrawsome.io.movie_metadata import MAX_SAMPLES
from mcrawsome.io.video_timing import presentation_times
from mcrawsome.output import chunks
from mcrawsome.output.chunks import InputFile, Manifest, WorkFolder
from mcrawsome.output.ffmpeg import ffprobe_for, find_ffmpeg, frame_rate
from mcrawsome.output.sidecar import sidecar_path
from mcrawsome.pipeline.render import FrameJob
from mcrawsome.pipeline.stabilise import (
    MotionOutcome,
    load_outcome,
    plan_raw_clip,
    plan_video,
    save_outcome,
)
from mcrawsome.pipeline.thermal import ThermalGuard, find_cpu_sensor
from mcrawsome.progress import Progress
from mcrawsome.recipe.schema import OutputParams, Recipe
from mcrawsome.recipe.times import parse_time
from mcrawsome.results import ClipResult

EXR_TARGETS = {"acescg": "ap1/linear", "linear-rec709": "rec709/linear"}  # output.exr_space to a key

DNG_ONLY = (
    "dng_shading",
    "dng_deflicker",
    "dng_transfer",
    "dng_camera_model",
    "dng_unique_model",
)  # output settings that only CinemaDNG uses


def ignored_settings(recipe: Recipe) -> list[str]:
    """Notes for the Recipe's settings that do nothing in its export."""
    kind, notes = recipe.output.kind, []
    defaults = OutputParams()
    changed = [name for name in DNG_ONLY if getattr(recipe.output, name) != getattr(defaults, name)]
    if kind != "dng" and changed:
        notes.append(f"{', '.join(f'output.{name}' for name in changed)}: CinemaDNG only; no effect here")
    if kind == "mcraw" and recipe.output.frame_rate != "keep":
        notes.append("output.frame_rate has no effect on MCRAW output: it copies the recorded frames")
    elif recipe.output.frame_rate != "keep" and not recipe.output.fill_dropped:
        notes.append("output.fill_dropped has no effect with a frame-rate conversion")
    if kind in ("dng", "mcraw") and recipe.develop.capture_sharpening > 0:
        notes.append("capture sharpening does not alter recorded sensor values in CinemaDNG or MCRAW")
    return notes


_FULL = "No space left on device"
FULL_DISK = (
    "the disk is full; what was finished is kept, so free some space and run the same command again to resume"
)


class ExportError(McError):
    """The export could not be completed; nothing was written to the requested path."""


@dataclass(frozen=True)
class ExportResult:
    output: Path
    sidecar: Path
    frames: int
    audio_offset_s: float | None
    warnings: list[str] = field(default_factory=list)

    def to_model(self, clip: Path) -> ClipResult:
        return ClipResult(
            clip=clip,
            output=self.output,
            sidecar=self.sidecar,
            frames=self.frames,
            audio_offset_s=self.audio_offset_s,
            notes=list(self.warnings),
        )


@dataclass(frozen=True)
class ClipFacts:
    width: int
    height: int
    neutral: tuple[float, float, float]
    measured_fps: float
    frame_count: int
    track: AudioTrack | None
    first_frame_ns: int
    sources: tuple[int, ...] | None = None  # each output frame's source frame when dropped frames are filled
    target: Fraction | None = None  # a frame-rate conversion's constant rate (output.frame_rate)
    crop: CropBox | None = (
        None  # the part of the raw frame used (develop.crop); width and height are its size
    )

    @property
    def rate(self) -> Fraction:
        return self.target if self.target is not None else frame_rate(self.measured_fps)

    @property
    def tempo(self) -> float:
        """How much faster the sound plays: the labelled rate over the measured one (at most 0.2%), or 1
        when the frame rate is converted (the frames follow the recorded times)."""
        return 1.0 if self.target is not None else float(self.rate) / self.measured_fps


def finish_interrupted_publish(
    output: Path, candidate: Callable[[Manifest], Manifest | None]
) -> ExportResult | None:
    """An export that died after publishing `output` but before its sidecar was moved into place: finish
    that, discard the work folder and say so. None when the output is not in that state."""
    work = WorkFolder(output)
    if not work.publish_interrupted(output):
        return None
    stored = work.load()
    if stored is None:
        return None
    wanted = candidate(stored)
    if wanted is None or not work.matches(wanted):
        return None
    sidecar = sidecar_path(output)
    if sidecar.exists():
        return None
    sidecar_path(work.publish_path).replace(sidecar)
    work.discard()
    try:
        measurements = tomllib.loads(sidecar.read_text(encoding="utf-8")).get("measurements", {})
    except (OSError, tomllib.TOMLDecodeError):
        measurements = {}
    offset = measurements.get("audio_offset_s")
    return ExportResult(
        output,
        sidecar,
        int(measurements.get("frames", 0)),
        float(offset) if isinstance(offset, (int, float)) else None,
        ["finished publishing the interrupted export"],
    )


def check_targets(output: Path, force: bool) -> None:
    for path in (output, sidecar_path(output)):
        if path.exists() and not force:
            raise ExportError(f"{path} already exists (use --force to replace it)", code="output-exists")


def clip_sources(
    clip: Clip, fill_dropped: bool, frame_rate: RateChoice = "keep", drift_ms: float = 20.0
) -> list[int]:
    """Each output frame's source frame. A frame-rate conversion maps a constant rate onto the recorded
    timestamps (repeating and skipping frames within `drift_ms`); otherwise, with `fill_dropped`, the frame
    before each gap in the timestamps is repeated for every frame missing there, so sound stays in sync."""
    target = target_rate(frame_rate, clip.timestamps_ns)
    if target is not None:
        return rate_map(clip.timestamps_ns, 1e9 / float(target), drift_ms * 1e6)
    if not fill_dropped:
        return list(range(clip.frame_count))
    return frame_map(clip.timestamps_ns, 1e9 / clip.fps)


def check_levels(clip_path: Path, frame: FrameMetadata, recipe: Recipe) -> None:
    """The Recipe's black and white level overrides must leave a range between them for this clip."""
    develop = recipe.develop.serving(frame)
    if develop.black_level is None and develop.white_level is None:
        return
    blacks, white = levels(frame, develop.black_level, develop.white_level)
    if white <= max(blacks):
        name = "develop.white_level" if develop.white_level is not None else "the clip's white level"
        raise ExportError(
            f"{clip_path.name}: {name} ({white:g}) must be above the black level ({max(blacks):g})",
            code="recipe-invalid",
        )


def read_clip_facts(clip_path: Path, recipe: Recipe, *, audio: bool = True) -> ClipFacts:
    """`frame_count` counts output frames: with dropped frames filled, more than the clip records."""
    if not clip_path.exists():
        raise ExportError(f"{clip_path} does not exist", code="input-missing")
    try:
        with Clip(clip_path) as clip:
            first = clip.frame_metadata(0)
            height, width = first.height, first.width
            check_levels(clip_path, first, recipe)
            blacks, white = levels(first, *recipe.develop.level_overrides(first))
            try:
                crop = clip_crop(clip, recipe.develop.crop, float(np.mean(blacks)), white)
            except CropError as exc:
                raise ExportError(f"{clip_path.name}: {exc}", code="recipe-invalid") from None
            if crop is not None:
                width, height = crop.width, crop.height
            if recipe.develop.rotation % 180:
                width, height = height, width
            output = recipe.output
            sources = clip_sources(clip, output.fill_dropped, output.frame_rate, output.frame_rate_drift_ms)
            try:
                neutral = clip_neutral(clip, recipe.develop, recipe.camera.colour_matrix)
            except ValueError as exc:
                raise ExportError(str(exc), code="recipe-invalid") from None
            return ClipFacts(
                width=width,
                height=height,
                neutral=neutral,
                measured_fps=clip.fps,
                frame_count=len(sources),
                track=clip.audio() if audio else None,
                first_frame_ns=int(clip.timestamps_ns[0]),
                sources=tuple(sources) if sources != list(range(clip.frame_count)) else None,
                target=target_rate(output.frame_rate, clip.timestamps_ns),
                crop=crop,
            )
    except (ContainerError, ValidationError, ValueError, RuntimeError, OSError) as exc:
        raise ExportError(
            f"{clip_path.name}: cannot read the clip ({exc})", code="input-unreadable"
        ) from None


def input_encoding_for(recipe: Recipe, identified: str | None) -> str | None:
    """The key of a video's colour encoding: the legacy develop.input_encoding, or develop.input_gamut
    and develop.input_transfer, each falling back to the identified file-name or metadata pair, with
    develop.input_ei for LogC3; None when unknown."""
    develop = recipe.develop
    if develop.input_encoding != "auto":
        return develop.input_encoding
    gamut: str = develop.input_gamut
    curve: str = develop.input_transfer
    if gamut == "auto" or curve == "auto":
        if identified is None:
            return None
        found = encoding(identified)
        gamut = found.gamut if gamut == "auto" else gamut
        curve = found.transfer if curve == "auto" else curve
    ei = develop.input_ei if curve == "logc3" else LOGC3_DEFAULT_EI
    return ColourEncoding(gamut, curve, ei).key


def read_video_facts(
    ffmpeg: Path, video: Path, recipe: Recipe, *, audio: bool = True
) -> tuple[ClipFacts, DirectLogInfo, str]:
    if not video.exists():
        raise ExportError(f"{video} does not exist", code="input-missing")
    try:
        info = probe_direct_log(ffprobe_for(ffmpeg), video)
    except DirectLogError as exc:
        raise ExportError(str(exc), code="input-unreadable") from None
    key = input_encoding_for(recipe, info.encoding)
    if key is None:
        raise ExportError(f"{video.name}: {UNKNOWN_COLOUR}", code="input-unreadable")
    try:
        track = read_audio(ffmpeg, info) if audio else None
    except DirectLogError as exc:
        raise ExportError(str(exc), code="input-unreadable") from None
    sources, target = video_sources(ffprobe_for(ffmpeg), info, recipe.output)
    facts = ClipFacts(
        width=info.display_size[0],
        height=info.display_size[1],
        neutral=(1.0, 1.0, 1.0),
        measured_fps=float(info.fps),
        frame_count=len(sources) if sources is not None else info.frame_count,
        track=track,
        first_frame_ns=0,
        sources=sources,
        target=target,
    )
    return facts, info, key


def video_sources(
    ffprobe: Path, info: DirectLogInfo, output: OutputParams
) -> tuple[tuple[int, ...] | None, Fraction | None]:
    """Map recorded presentation times onto the editor/export clock, without a transcode.

    Keep uses a VFR video's nominal rate, repeating/skipping pictures at their
    recorded times. Explicit conversion choices also work on constant-rate LOG.
    """
    if output.frame_rate == "keep" and not info.variable_frame_rate:
        return None, None
    times = presentation_times(ffprobe, info.path)
    target = target_rate(output.frame_rate, times) or info.fps
    last_hold = float(np.median(np.diff(times))) if len(times) > 1 else 1e9 / float(info.fps)
    if (float(times[-1]) + last_hold) * float(target) / 1e9 > MAX_SAMPLES:
        raise ExportError("LOG conversion would exceed two million output frames", code="recipe-invalid")
    sources = rate_map(times, 1e9 / float(target), output.frame_rate_drift_ms * 1e6)
    mapped = tuple(sources) if sources != list(range(info.frame_count)) else None
    return mapped, target


def stops(recipe: Recipe) -> float:
    """The Recipe's exposure; an unresolved "auto" counts as 0 until resolve_auto replaces it."""
    exposure = recipe.develop.exposure
    return 0.0 if exposure == "auto" else float(exposure)


@contextlib.contextmanager
def failing_as(clip_path: Path, what: str, code: ErrorCode) -> Iterator[None]:
    """Any error inside becomes this clip's ExportError, so a batch reports it and carries on."""
    try:
        yield
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"{clip_path.name}: {what} failed ({exc})", code=code) from None


def frame_range_for(
    clip_path: Path,
    start: str | None,
    duration: str | None,
    fill_dropped: bool = True,
    *,
    frame_rate: RateChoice = "keep",
    drift_ms: float = 20.0,
) -> tuple[tuple[int, int] | None, str | None]:
    """(first frame, frame count) for --start and --duration, and a note when the range was cut short.
    A start after the clip's end is an error. Frames are output frames (`fill_dropped`, `frame_rate` and
    `drift_ms` as the Recipe's)."""
    if start is None and duration is None:
        return None, None
    if is_video_input(clip_path):
        ffprobe = find_ffmpeg()[1]
        info = probe_direct_log(ffprobe, clip_path)
        sources, target = video_sources(
            ffprobe, info, OutputParams(frame_rate=frame_rate, frame_rate_drift_ms=drift_ms)
        )
        total, fps = len(sources) if sources is not None else info.frame_count, float(target or info.fps)
    else:
        with Clip(clip_path) as clip:
            total = len(clip_sources(clip, fill_dropped, frame_rate, drift_ms))
            target = target_rate(frame_rate, clip.timestamps_ns)
            fps = float(target) if target is not None else clip.fps
    try:
        first = parse_time(start, fps) if start else 0
        requested = parse_time(duration, fps) if duration else None
    except ValueError as exc:
        raise ExportError(f"{clip_path.name}: {exc}", code="recipe-invalid") from None
    if first >= total:
        raise ExportError(
            f"{clip_path.name}: --start {start} is after the end of the clip ({total} frames)",
            code="recipe-invalid",
        )
    wanted = requested if requested is not None else total - first
    if wanted < 1:
        raise ExportError(
            f"{clip_path.name}: --duration {duration} is shorter than one frame", code="recipe-invalid"
        )
    count = min(wanted, total - first)
    note = f"the clip ends after {count} frames of the requested {wanted}" if count < wanted else None
    return (first, count), note


def trimmed_range(
    clip_path: Path,
    recipe: Recipe,
    start: str | None,
    duration: str | None,
    fill_dropped: bool,
    frame_rate: RateChoice = "keep",
) -> tuple[tuple[int, int] | None, str | None]:
    """frame_range_for with --start and --duration each falling back to the Recipe's [trim]."""
    return frame_range_for(
        clip_path,
        start if start is not None else recipe.trim.start,
        duration if duration is not None else recipe.trim.duration,
        fill_dropped,
        frame_rate=frame_rate,
        drift_ms=recipe.output.frame_rate_drift_ms,
    )


def trim_facts(clip_path: Path, facts: ClipFacts, first: int, count: int) -> ClipFacts:
    if is_video_input(clip_path):
        first_ns = facts.first_frame_ns + round(first / float(facts.rate) * 1e9)
    else:
        with Clip(clip_path) as clip:
            if facts.target is not None:  # output frames are on the converted rate's timeline
                first_ns = facts.first_frame_ns + round(first * 1e9 / float(facts.target))
            elif facts.sources is None:
                first_ns = int(clip.timestamps_ns[first])
            else:  # a repeat is shown where the dropped frame would have been
                source = facts.sources[first]
                behind = first - facts.sources.index(source)
                first_ns = int(clip.timestamps_ns[source]) + round(behind * 1e9 / facts.measured_fps)
    return dataclasses.replace(facts, frame_count=count, first_frame_ns=first_ns)


def filled_frames(facts: ClipFacts, first: int) -> tuple[dict[str, Any], str | None]:
    """The sidecar's record of a RAW export's frames from output frame `first` (trimmed facts), with
    `repeated` numbered from the export's first frame, and a note when dropped frames were filled."""
    outputs = range(first, first + facts.frame_count)
    sources = facts.sources if facts.sources is not None else range(outputs.stop)
    repeated = [o - first for o in outputs if o > 0 and sources[o] == sources[o - 1]]
    record: dict[str, Any] = {
        "source_count": len({sources[o] for o in outputs}),
        "output_count": facts.frame_count,
        "repeated": repeated,
    }
    if facts.target is not None:
        shown = [sources[o] for o in outputs]
        record["dropped"] = sum(max(0, b - a - 1) for a, b in itertools.pairwise(shown))
        rate = f"{float(facts.target):.3f}".rstrip("0").rstrip(".")
        return (
            record,
            f"converted to {rate} fps: repeated {len(repeated)} frames and dropped {record['dropped']}",
        )
    if not repeated:
        return record, None
    times = [f"{o / float(facts.rate):.1f} s" for o in repeated[:10]]
    if len(repeated) > 10:
        times.append(f"and {len(repeated) - 10} more")
    frames = f"{len(repeated)} dropped frame{'s' if len(repeated) > 1 else ''}"
    return record, f"filled {frames} (at {', '.join(times)}) so sound stays in sync"


def native_matrix(clip_path: Path, recipe: Recipe, neutral: tuple[float, float, float]) -> tuple[float, ...]:
    """AP1 to the camera's white-balanced RGB for a RAW clip: the inverse of the developer's matrix, so a
    Camera Native output undoes exactly the conversion development made."""
    with Clip(clip_path) as clip:
        container = clip.metadata
    if recipe.camera.colour_matrix is not None:
        container = with_colour_matrix(container, recipe.camera.colour_matrix)
    forward, _ = camera_to_ap1(container, neutral)
    return tuple(float(v) for v in np.linalg.inv(forward.astype(np.float64)).ravel())


def frame_job(clip_path: Path, recipe: Recipe, facts: ClipFacts) -> FrameJob:
    """The clip's frame job: OpenEXR in its scene-linear space, otherwise ACEScct. `facts` are the whole
    clip's (before trimming): units are planned on all of it."""
    exr = recipe.output.kind == "exr"
    black, white = recipe.develop.black_level, recipe.develop.white_level
    if recipe.develop.level_policy == "recorded":
        black, white = None, None
    elif recipe.develop.level_policy == "static" and not is_video_input(clip_path):
        with Clip(clip_path) as clip:
            black, white = recipe.develop.level_overrides(clip.frame_metadata(0))
    return FrameJob(
        clip_path=clip_path,
        exposure_stops=stops(recipe),
        neutral=facts.neutral,
        target=EXR_TARGETS[recipe.output.exr_space] if exr else "acescct",
        float_output=exr,
        fps=float(facts.rate) if facts.target is not None else facts.measured_fps,
        highlight_recovery=recipe.develop.highlight_recovery,
        lens_shading=recipe.develop.lens_shading,
        capture_sharpening=recipe.develop.capture_sharpening if not is_video_input(clip_path) else 0.0,
        capture_radius=recipe.develop.capture_radius,
        clip_frames=facts.frame_count,
        sources=facts.sources,
        black_level=black,
        white_level=white,
        dynamic_levels=recipe.develop.level_policy == "recorded",
        lens_shading_mode=recipe.develop.lens_shading_mode,
        vignette_profile=recipe.develop.vignette_profile,
        rotation=recipe.develop.rotation,
        colour_matrix=recipe.camera.colour_matrix,
        crop=dataclasses.astuple(facts.crop) if facts.crop is not None else None,
        cfa_layout=recipe.develop.cfa_layout,
        cfa_pattern=recipe.develop.cfa_pattern,
    )


VideoSource = tuple[DirectLogInfo, str]  # a video's probe and the key of its colour encoding


def source_job(
    clip_path: Path, recipe: Recipe, facts: ClipFacts, video: VideoSource | None = None
) -> FrameJob:
    """frame_job, with a video's decoding (its encoding, recorded size and display turn) when it is one."""
    job = frame_job(clip_path, recipe, facts)
    if video is None:
        return job
    info, key = video
    native_in, native_out = (k.startswith(f"{CAMERA_NATIVE}/") for k in (key, job.target))
    if native_out and not native_in:
        raise ExportError(
            f"{clip_path.name}: a Camera Native output needs a RAW clip or a Camera Native video; this video "
            f"is {encoding(key).name}, and a video has no camera matrix to undo",
            code="recipe-invalid",
        )
    matrix = None
    if native_in and not native_out:
        if recipe.camera.colour_matrix is not None:
            matrix = tuple(float(v) for v in native_to_ap1(recipe.camera.colour_matrix).ravel())
        elif recipe.develop.native_as != "ap1":
            raise ExportError(
                f"{clip_path.name}: this video is in Camera Native, the phone's own colours, and has no "
                "matrix to convert them; set develop.native_as = ap1 to read them as ACES AP1, or "
                "camera.colour_matrix to the phone's colour matrix (Camera Native log output needs neither)",
                code="recipe-invalid",
            )
    return dataclasses.replace(
        job,
        source="video",
        input_encoding=key,
        input_matrix=matrix,
        size=(info.width, info.height),
        video_codec=info.codec,
        rotation=info.rotation,
    )


def read_source(
    clip_path: Path, recipe: Recipe, *, audio: bool = True
) -> tuple[ClipFacts, VideoSource | None]:
    """A clip's facts, and for a video its probe and colour encoding."""
    if is_video_input(clip_path):
        facts, info, key = read_video_facts(find_ffmpeg()[0], clip_path, recipe, audio=audio)
        return facts, (info, key)
    return read_clip_facts(clip_path, recipe, audio=audio), None


def clip_job(clip_path: Path, recipe: Recipe) -> tuple[FrameJob, int, tuple[int, int]]:
    """The FrameJob an export would build for this clip, its frame count and its (display) frame size."""
    facts, video = read_source(clip_path, recipe)
    return source_job(clip_path, recipe, facts, video), facts.frame_count, (facts.width, facts.height)


def picture_key(job: FrameJob) -> str:
    """A fingerprint of everything that decides the picture, so a changed setting starts the export again."""
    settings = {
        **({"video_timing_version": 2} if job.source == "video" else {}),
        "raw_decode_version": RAW_DECODE_VERSION if job.source == "raw" else None,
        "quad_bayer_remosaic": job.quad_bayer_remosaic if job.source == "raw" else None,
        "target": job.target,
        "input_encoding": job.input_encoding,
        "rotation": job.rotation,
        "highlight_recovery": job.highlight_recovery,
        "lens_shading": job.lens_shading,
        **raw_settings(job),
        **(
            {"capture_sharpening": job.capture_sharpening, "capture_radius": job.capture_radius}
            if job.capture_sharpening > 0
            else {}
        ),
    }
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def raw_settings(job: FrameJob) -> dict[str, Any]:
    """The Milestone 6 raw settings that are set, for the picture fingerprint: left out at their defaults, so
    an export interrupted before they existed still resumes."""
    settings = {
        "dynamic_levels": job.dynamic_levels,
        "black_level": job.black_level,
        "white_level": job.white_level,
        "lens_shading_mode": job.lens_shading_mode if job.lens_shading_mode != "full" else None,
        "colour_matrix": job.colour_matrix,
        "vignette_profile": job.vignette_profile.model_dump(mode="json") if job.vignette_profile else None,
        "crop": job.crop,
        "cfa_layout": job.cfa_layout if job.cfa_layout != "auto" else None,
        "cfa_pattern": job.cfa_pattern if job.cfa_pattern != "recorded" else None,
    }
    return {key: value for key, value in settings.items() if value is not None}


def is_disk_full(exc: BaseException) -> bool:
    """An OSError with ENOSPC, or an error whose message is the system's "No space left on device"."""
    return (isinstance(exc, OSError) and exc.errno == errno.ENOSPC) or _FULL in str(exc)


def thermal_guard(limit: float | None) -> ThermalGuard | None:
    """A guard for the CPU temperature limit, or None when the export has none."""
    return None if limit is None else ThermalGuard(limit, find_cpu_sensor())


def base_measurements(job: FrameJob, facts: ClipFacts, offset: float | None) -> dict[str, Any]:
    measurements: dict[str, Any] = {
        "audio_offset_s": offset if offset is not None else "none",
        "frames": facts.frame_count,
        "measured_fps": facts.measured_fps,
        "white_balance_neutral": list(facts.neutral),
    }
    if facts.target is not None:
        measurements["frame_rate_target"] = str(facts.target)
    if facts.crop is not None:
        measurements["crop"] = str(facts.crop)
    if job.capture_sharpening > 0:
        measurements["capture_sharpening"] = {"amount": job.capture_sharpening, "radius": job.capture_radius}
    return measurements


def motion_key(clip_path: Path, recipe: Recipe) -> str:
    """What the motion step depends on: the clip's fingerprint and the Recipe's [motion] and [camera]
    values, with whether the output is stabilised at all."""
    gyro = chunks.gyro_input(clip_path, recipe)
    settings = {
        "stabilisation_version": 5,
        **({"video_timing_version": 2} if is_video_input(clip_path) else {}),
        "input": InputFile.of(clip_path).model_dump(mode="json"),
        "gyro": gyro.model_dump(mode="json") if gyro is not None else None,
        "motion": recipe.motion.model_dump(mode="json"),
        "camera": recipe.camera.model_dump(mode="json"),
        "camera_profiles": [p.model_dump(mode="json") for p, _ in all_profiles()]
        if recipe.motion.stabilise != "off"
        else [],
    }
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def plan_motion(
    clip_path: Path,
    recipe: Recipe,
    video_info: DirectLogInfo | None,
    ffmpeg: Path,
    workers: int,
    staging: Path,
    cache: Path | None = None,
) -> tuple[MotionOutcome, dict[str, str]]:
    """The motion step (the plan goes into `staging`) and the video's telemetry device fields. With a
    `cache` folder, a result planned before for the same clip and settings is reused."""
    kept = cache / motion_key(clip_path, recipe) if cache is not None else None
    if kept is not None and (found := load_outcome(kept, staging)) is not None:
        return found
    device: dict[str, str] = {}
    if video_info is not None:
        motion, device = plan_video(ffmpeg, video_info, recipe, workers, staging)
    else:
        motion = plan_raw_clip(clip_path, recipe, workers, staging)
    if kept is not None:
        save_outcome(motion, device, kept)
    return motion, device


def _prepare(
    clip_path: Path,
    recipe: Recipe,
    whole_job: FrameJob,
    whole_frames: int,
    video_info: DirectLogInfo | None,
    ffmpeg: Path,
    frame_range: tuple[int, int] | None,
    workers: int,
    render_workers: int,
    folder: Path,
    progress: Progress | None,
) -> tuple[Recipe, dict[str, Any], list[str]]:
    """Measure the whole clip: the resolved Recipe, the measurements and the notes. Exports are not
    stabilised, so no motion is planned."""
    if progress:
        progress("measuring", 0, 0)
    with failing_as(clip_path, "measuring the clip", "measure-failed"):
        resolution = resolve_auto(recipe, whole_job, whole_frames, workers, progress=progress)
    recipe = resolution.recipe
    measurements = dict(resolution.measurements)
    if frame_range is not None:
        measurements["first_frame"] = frame_range[0]
    notes = list(resolution.notes)
    if video_info is None:
        with Clip(clip_path) as clip:
            notes += [note for note in (clip.recovery_note, clip.remosaic_note) if note]
    if whole_job.capture_sharpening > 0:
        notes.append(
            "capture sharpening needs a recorded RAW noise profile; frames without one are unchanged"
        )
    if resolution.stats is not None:
        with failing_as(clip_path, "measuring the white balance", "measure-failed"):
            wb = white_balance_spread(clip_path)
        advice, suggestions = sample_advice(resolution.stats, recipe, wb, raw=video_info is None)
        notes += advice + suggestions
    if (
        video_info is not None
        and whole_job.input_encoding.startswith(f"{CAMERA_NATIVE}/")
        and not whole_job.target.startswith(f"{CAMERA_NATIVE}/")
    ):
        notes.append(
            "a Camera Native video: its colours are converted with camera.colour_matrix"
            if whole_job.input_matrix is not None
            else "a Camera Native video: its colours are read as ACES AP1 (develop.native_as = ap1)"
        )
    if video_info is not None and recipe.develop.highlight_recovery != 1.0:
        notes.append("highlight recovery needs RAW input; it does nothing for a video")
    if video_info is not None and recipe.develop.deflicker > 0:
        notes.append("deflicker needs RAW input; it does nothing for a video")
    if video_info is not None and video_info.variable_frame_rate:
        notes.append("variable-rate LOG: pictures follow recorded timestamps on the fixed-rate output clock")
    if video_info is not None and (recipe.develop.black_level, recipe.develop.white_level) != (None, None):
        notes.append("black and white level overrides need RAW input; they do nothing for a video")
    return recipe, measurements, notes
