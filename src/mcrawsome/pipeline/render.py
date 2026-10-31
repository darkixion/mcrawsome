"""Per-frame processing: raw -> developed AP1 -> stabilisation, turn and exposure -> encoding, 16-bit (or
scene linear float32, for OpenEXR)."""

from __future__ import annotations

import atexit
import contextlib
from collections import deque
from collections.abc import Callable, Generator, Iterator, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from multiprocessing.pool import AsyncResult
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.camera.colour import with_colour_matrix
from mcrawsome.camera.shading import ShadingMode
from mcrawsome.camera.vignette import VignetteProfile, apply_profile
from mcrawsome.colour.encoding import from_linear_ap1, to_linear_ap1
from mcrawsome.develop.crop import CropBox, crop_data, crop_metadata
from mcrawsome.develop.noise import NoiseModel, recorded_noise_model
from mcrawsome.develop.raw import Developer, median_neutral
from mcrawsome.develop.sharpen import capture_sharpen
from mcrawsome.io.clip import CfaLayout, CfaPattern, Clip, close_all, remosaic_enabled
from mcrawsome.io.metadata import FrameMetadata
from mcrawsome.io.vaapi import decode_device
from mcrawsome.motion.geometry import warp_maps
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.pipeline.pool import bounded_imap, result_of, settle, worker_pool, workers_of
from mcrawsome.pipeline.shared import FrameRing, VideoDecoder, read_slot
from mcrawsome.pipeline.thermal import ThermalGuard
from mcrawsome.pipeline.units import SlotBook, Unit, plan_units

Image = npt.NDArray[np.float32]


@dataclass(frozen=True)
class FrameJob:
    clip_path: Path
    exposure_stops: float
    neutral: tuple[float, float, float]
    target: str = "acescct"  # the colour encoding's key (mcrawsome.colour.encoding)
    fps: float = 24.0
    plan_path: Path | None = None  # a saved StabilisationPlan; None leaves the frame as recorded
    source: Literal["raw", "video"] = "raw"  # a .mcraw clip, or a decoded video (Direct Log)
    input_encoding: str = "acescct"  # for video sources: the key of the file's colour encoding
    input_matrix: tuple[float, ...] | None = None  # a Camera Native video: its RGB to AP1, row by row
    size: tuple[int, int] | None = None  # (width, height), needed for video sources
    video_codec: str | None = None  # a video source's codec (FFmpeg's name), to choose its decoder
    rotation: int = 0  # clockwise display turn, after stabilisation in recorded orientation
    highlight_recovery: float = 1.0  # RAW only: rebuild clipped channels from the others
    lens_shading: float = 1.0  # RAW only: 1 corrects the lens vignette fully, 0 keeps it
    first_frame: int = 0  # a trimmed render: frame 0 of iter_frames is this source frame
    clip_frames: int | None = None  # the source clip's length; None means the rendered range is all of it
    sources: tuple[int, ...] | None = None  # each output frame's source frame when dropped frames are filled
    black_level: float | tuple[float, float, float, float] | None = None  # RAW only: replaces the clip's
    white_level: float | None = None  # RAW only: replaces the clip's
    dynamic_levels: bool = False  # explicit recorded policy; legacy keeps its historical output
    vignette_profile: VignetteProfile | None = None
    lens_shading_mode: ShadingMode = "full"  # RAW only: "colour" corrects the colour shading only
    colour_matrix: tuple[float, ...] | None = None  # RAW only: XYZ to camera, replacing the clip's matrices
    crop: tuple[int, int, int, int] | None = None  # RAW only: (left, top, width, height) of the raw frame
    frame_gains: tuple[float, ...] | None = None  # deflicker: a gain for each source frame
    float_output: bool = False  # scene-linear float32 in the target's gamut (OpenEXR), not 16-bit codes
    native_matrix: tuple[float, ...] | None = (
        None  # Camera Native output: AP1 to the camera's RGB, row by row
    )
    quad_bayer_remosaic: bool = field(default_factory=remosaic_enabled)
    cfa_layout: CfaLayout = "auto"  # RAW only: develop.cfa_layout
    cfa_pattern: CfaPattern = "recorded"  # RAW only: develop.cfa_pattern
    capture_sharpening: float = 0.0
    capture_radius: float = 0.8


_HEADROOM = 1.6  # a worker's peak over the frames it holds
_MEMORY_SHARE = 0.6  # of the memory available when the render starts


def available_memory() -> int | None:
    """Bytes the system can give without swapping (Linux MemAvailable), or None when unknown."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def worker_budget(
    job: FrameJob, size: tuple[int, int], requested: int, available: int | None
) -> tuple[int, str | None]:
    """Workers for a render that sharpens, which holds noise-model planes per worker, so the render stays
    within the memory available. Other renders keep the requested count."""
    if available is None or job.capture_sharpening <= 0:
        return requested, None
    per_worker = 3 * size[0] * size[1] * 3 * 4 * _HEADROOM + size[0] * size[1] * 4 * 16
    allowed = max(1, int(_MEMORY_SHARE * available // per_worker))
    if allowed >= requested:
        return requested, None
    note = (
        f"using {allowed} workers instead of {requested}: sharpening holds several planes per worker, "
        f"and {available / 2**30:.0f} GB of memory is available"
    )
    return allowed, note


def choose_neutral(clip: Clip, policy: Literal["median", "first"]) -> tuple[float, float, float]:
    if policy == "first":
        return clip.frame_metadata(0).as_shot_neutral
    step = max(1, clip.frame_count // 60)
    return median_neutral([clip.frame_metadata(i).as_shot_neutral for i in range(0, clip.frame_count, step)])


_state: dict[Path, tuple[Clip, Developer]] = {}


def open_clip(job: FrameJob) -> Clip:
    """The job's clip, read with its Quad Bayer and colour filter settings."""
    return Clip(
        job.clip_path,
        quad_bayer_remosaic=job.quad_bayer_remosaic,
        cfa_layout=job.cfa_layout,
        cfa_pattern=job.cfa_pattern,
    )


def reads_as(clip: Clip, job: FrameJob) -> bool:
    """Whether an open clip reads frames as `job` asks."""
    return (clip.quad_bayer_remosaic, clip.cfa_layout, clip.cfa_pattern) == (
        job.quad_bayer_remosaic,
        job.cfa_layout,
        job.cfa_pattern,
    )


@atexit.register
def _close_clips() -> None:
    """Close the clips kept open for the process (a worker's, or the caller's when run in-process)."""
    clips = [clip for clip, _ in _state.values()]
    _state.clear()
    close_all(clips)


def _worker(job: FrameJob, index: int | None = None) -> tuple[Clip, Developer]:
    if job.clip_path in _state and not reads_as(_state[job.clip_path][0], job):
        _state.pop(job.clip_path)[0].close()
    if job.clip_path not in _state:
        clip = open_clip(job)
        container = clip.metadata
        if job.colour_matrix is not None:
            container = with_colour_matrix(container, job.colour_matrix)
        developer = Developer(
            container,
            first_metadata(job, clip),
            job.neutral,
            job.highlight_recovery,
            job.lens_shading,
            shading_mode=job.lens_shading_mode,
            black_level=job.black_level,
            white_level=job.white_level,
        )
        _state[job.clip_path] = (clip, developer)
    clip, developer = _state[job.clip_path]
    if job.dynamic_levels and index is not None:
        developer = developer.with_levels(clip.frame_metadata(index))
        _state[job.clip_path] = (clip, developer)
    return clip, developer


_plans: dict[Path, StabilisationPlan] = {}


def _stabilise(linear: Image, plan_path: Path, index: int) -> Image:
    """Turn the frame by the plan's per-row rotations, in linear light (prototype render.py)."""
    if plan_path not in _plans:
        _plans[plan_path] = StabilisationPlan.load(plan_path)
    plan = _plans[plan_path]
    height, width = linear.shape[:2]
    mx, my = warp_maps(plan.rotations[index].astype(np.float64), plan.focal_px, plan.zoom, width, height)
    if plan.rotate_180:  # the plan is in sensor orientation; this frame was already turned upright
        mx, my = (width - 1) - mx[::-1, ::-1], (height - 1) - my[::-1, ::-1]
    warped = cv2.remap(linear, mx, my, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT)
    return np.asarray(warped, dtype=np.float32)


def first_metadata(job: FrameJob, clip: Clip) -> FrameMetadata:
    """The first frame's metadata, for the job's crop of the raw frame."""
    first = apply_profile(clip.metadata, clip.frame_metadata(0), job.vignette_profile)
    return crop_metadata(first, CropBox(*job.crop)) if job.crop is not None else first


def _develop_raw(job: FrameJob, index: int) -> Image:
    clip, developer = _worker(job, index)
    data = clip.raw_frame(index).data
    return developer.develop(crop_data(data, CropBox(*job.crop)) if job.crop is not None else data)


def video_linear(job: FrameJob, rgb: npt.NDArray[np.uint16]) -> Image:
    linear = to_linear_ap1(rgb.astype(np.float32) / np.float32(65535), job.input_encoding)
    if job.input_matrix is None:
        return linear
    return np.asarray(linear @ np.asarray(job.input_matrix, np.float32).reshape(3, 3).T, np.float32)


Frame = npt.NDArray[np.uint16 | np.float32]  # 16-bit codes, or scene linear with float_output


def _sharpened(job: FrameJob, linear: Image, index: int, gain: float = 1.0) -> Image:
    """Capture sharpening of a RAW frame, held back where the recorded noise would be raised."""
    if job.capture_sharpening <= 0 or job.source != "raw":
        return linear
    clip, developer = _worker(job)
    model = recorded_noise_model(developer, clip.frame_metadata(index).noise_profile)
    if model is not None and gain != 1:
        model = NoiseModel(
            np.asarray(model.scale, np.float32) * gain, np.asarray(model.offset, np.float32) * gain**2
        )
    return capture_sharpen(linear, model, job.capture_sharpening, job.capture_radius)


def render_frame(job: FrameJob, index: int) -> Frame:
    """One frame of a RAW clip, developed and finished."""
    return finish_frame(job, _sharpened(job, _develop_raw(job, index), index), index)


def render_video_frame(job: FrameJob, rgb: npt.NDArray[np.uint16], index: int) -> Frame:
    """One decoded video frame (16-bit, in the file's colour encoding), converted and finished."""
    return finish_frame(job, video_linear(job, rgb), index)


def render_unit(job: FrameJob, linear: Image, unit: Unit) -> Frame:
    """A unit's output frame from its source frame (scene-linear AP1), shown `unit.repeats` times."""
    gain = 1.0
    if job.frame_gains is not None:
        gain = job.frame_gains[unit.output_index]
        linear = linear * np.float32(gain)
        job = replace(job, frame_gains=None)  # corrected before sharpening; do not apply twice
    return finish_frame(job, _sharpened(job, linear, unit.output_index, gain), unit.output_index)


def finish_frame(job: FrameJob, linear: Image, index: int) -> Frame:
    """Scene-linear AP1 to the job's 16-bit output: stabilise, turn, exposure, encode. `index` is the
    source frame (for stabilisation). With `float_output`, scene linear in the target's gamut (float32,
    unclipped)."""
    if job.plan_path is not None:
        linear = _stabilise(linear, job.plan_path, index)
    if job.rotation:
        linear = np.rot90(linear, -job.rotation // 90)
    gain = 2.0**job.exposure_stops
    if job.frame_gains is not None:
        gain *= job.frame_gains[index]
    linear = linear * np.float32(gain)
    if job.float_output:
        return np.asarray(from_linear_ap1(linear, job.target), dtype=np.float32)
    native = None if job.native_matrix is None else np.asarray(job.native_matrix).reshape(3, 3)
    encoded = from_linear_ap1(linear, job.target, native)
    return np.asarray(np.clip(encoded, 0.0, 1.0) * 65535.0 + 0.5, dtype=np.uint16)


Finish = Callable[[Frame], Any]


def _task(job: FrameJob, finish: Finish | None, ring: str | None, slot: int | None, unit: Unit) -> Any:
    if ring is None:
        linear = _develop_raw(job, unit.output_index)
    else:
        assert job.size is not None
        assert slot is not None
        linear = video_linear(job, read_slot(ring, slot, (job.size[1], job.size[0], 3)))
    rendered = render_unit(job, linear, unit)
    return finish(rendered) if finish else rendered


def iter_frames(
    job: FrameJob,
    frame_count: int,
    workers: int,
    indices: Sequence[int] | None = None,
    finish: Finish | None = None,
    emit: tuple[int, int] | None = None,
    guard: ThermalGuard | None = None,
) -> Generator[Any, None, None]:
    """Output frames (or `finish(frame)` results, computed in the workers) in order, with a fixed number
    of units in flight. The export range is `frame_count` frames from `job.first_frame`; `emit =
    (start, count)`, relative to that range, gives only those output frames. A unit's frame is repeated
    once per output frame it fills (frame-rate conversion and dropped frames), except when sampling
    `indices` (relative to the export range; those outside `emit` are skipped), where each unit comes back
    once. Video sources are decoded here and handed over through shared memory. With a `guard`, each unit
    waits for the CPU to be cool enough before it is submitted."""
    start, count = emit if emit is not None else (0, frame_count)
    if emit is not None and (start < 0 or count < 1 or start + count > frame_count):
        raise ValueError(f"emit range ({start}, {count}) is outside the {frame_count} frames rendered")
    clip_frames = job.clip_frames if job.clip_frames is not None else job.first_frame + frame_count
    if clip_frames < job.first_frame + frame_count:
        raise ValueError(
            f"{frame_count} frames from frame {job.first_frame} run past the clip's {clip_frames} frames"
        )
    span = (job.first_frame + start, count)
    sampled = None if indices is None else [job.first_frame + i for i in indices if 0 <= i < frame_count]
    units = plan_units(clip_frames, 0, 1, sampled, span, job.sources)
    repeat = indices is None
    window = workers * 2

    def cooled() -> Iterator[Unit]:
        for unit in units:
            if guard is not None:
                guard.wait()
            yield unit

    def repeated(value: Any, unit: Unit) -> Iterator[Any]:
        for _ in range(unit.repeats if repeat else 1):
            yield value

    with worker_pool(workers) as pool:
        if job.source == "raw":
            task = partial(_task, job, finish, None, None)
            with contextlib.closing(bounded_imap(pool, task, cooled(), window=window)) as results:
                for unit, value in zip(units, results, strict=True):
                    yield from repeated(value, unit)
            return
        assert job.size is not None
        width, height = job.size
        processes = workers_of(pool)  # so a wait can tell that one has died
        slots = window + 1  # bounded whatever the clip length
        book = SlotBook(slots, units)
        ffmpeg = find_ffmpeg()[0]
        with (
            FrameRing(slots, (height, width, 3)) as ring,
            VideoDecoder(
                ffmpeg,
                job.clip_path,
                job.size,
                device=decode_device(ffmpeg, job.video_codec),
                codec=job.video_codec,
            ) as decoder,
        ):
            pending: deque[tuple[AsyncResult[Any], Unit, int]] = deque()
            scratch = np.empty((height, width, 3), np.uint16)
            decoded = 0  # the index of the next frame the decoder gives

            def oldest() -> tuple[Any, Unit]:
                unit, position = pending[0][1:]
                value = result_of(pending.popleft()[0], processes)  # not held in a variable: see result_of
                book.done(position)
                return value, unit

            try:
                for position, unit in enumerate(units):
                    if guard is not None:
                        guard.wait()
                    while len(pending) >= window:
                        yield from repeated(*oldest())
                    frame = unit.output_index
                    if book.slot_of(frame) is None:
                        while decoded < frame:  # frames no unit needs
                            if not decoder.read_into(scratch):
                                raise ValueError(f"{job.clip_path.name} ended before frame {frame}")
                            decoded += 1
                        while not book.has_free():
                            yield from repeated(*oldest())
                        if not decoder.read_into(ring.view(book.acquire(frame))):
                            raise ValueError(f"{job.clip_path.name} ended before frame {frame}")
                        decoded += 1
                    slot = book.slot_of(frame)
                    assert slot is not None
                    pending.append(
                        (pool.apply_async(_task, (job, finish, ring.name, slot, unit)), unit, position)
                    )
                while pending:
                    yield from repeated(*oldest())
            finally:  # stopped early or a unit failed: see settle
                settle((result for result, _, _ in pending), workers=processes)
