"""Preview sessions: one frame developed at preview size and kept, then re-finished on each setting change.

A seek develops the frame (RAW at half size, one pixel per Bayer quad); exposure and the neutral display
rendering (colour/display.py) run again on the kept frame, so a slider moves without developing anything
(preview/finish.py's fast path). `refined` gives the full-quality frame, with sharpening and stabilisation.
An automatic exposure shows as 0 stops until it is measured (`auto_measurer`, as an export measures it;
the preview worker does it in the background). Previews are 8-bit JPEGs in BT.709 code values.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import json
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, Field

from mcrawsome import _raw
from mcrawsome.analysis.resolve import resolve_auto
from mcrawsome.camera.colour import with_colour_matrix
from mcrawsome.colour.cube import Table
from mcrawsome.colour.display import display_table
from mcrawsome.develop.crop import CropBox, crop_data
from mcrawsome.develop.raw import Developer, Image
from mcrawsome.io.clip import Clip, RawFrame, remosaic_enabled
from mcrawsome.output.export import (
    ClipFacts,
    motion_key,
    plan_motion,
    read_source,
    source_job,
)
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.paths import cache_dir
from mcrawsome.pipeline.priority import background_priority
from mcrawsome.pipeline.render import FrameJob, first_metadata, iter_frames, open_clip, video_linear
from mcrawsome.preview.backend import PreviewBackend, gpu_enabled
from mcrawsome.preview.cache import FrameCache
from mcrawsome.preview.display import DisplayFrames, DisplayLease
from mcrawsome.preview.finish import Finisher, codes16, display_bgr
from mcrawsome.preview.prefetch import FastPipeline
from mcrawsome.preview.video import VideoFrames
from mcrawsome.recipe.schema import Recipe
from mcrawsome.runtime import default_workers


@dataclass(frozen=True)
class Refined:
    """A refined frame, and why it is not stabilised when it is not (the motion step's notes)."""

    jpeg: bytes
    notes: list[str] = field(default_factory=list)
    size: tuple[int, int] | None = None
    display: DisplayLease | None = None
    release: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def discard(self) -> None:
        if self.release is not None:
            self.release()


class PreviewSettings(BaseModel):
    quad_bayer_remosaic: bool = Field(default_factory=remosaic_enabled)
    width: int = Field(1920, ge=16, description="Maximum preview width in pixels")
    fast_width: int = Field(1280, ge=16, le=1920, description="Preferred fast playback width")
    gpu: bool = Field(
        default_factory=gpu_enabled, description="Use an available OpenCL GPU for fast previews"
    )
    quality: int = Field(85, ge=1, le=100, description="JPEG quality")
    cache_mib: int = Field(256, ge=0, le=2048, description="Scrub cache memory in MiB; 0 disables it")
    fast_open: bool = False


def _resized(image: npt.NDArray[Any], size: tuple[int, int]) -> npt.NDArray[Any]:
    """`image` at (width, height), averaging areas when it shrinks."""
    if (image.shape[1], image.shape[0]) == size:
        return image
    return np.asarray(cv2.resize(image, size, interpolation=cv2.INTER_AREA))


def _preview_bgr(frame: npt.NDArray[np.uint16], table: Table, size: tuple[int, int]) -> npt.NDArray[np.uint8]:
    """A rendered full-size frame at preview size through the display table, as 8-bit BGR (run in a render
    worker)."""
    return display_bgr(_resized(frame, size).astype(np.float32) / np.float32(65535), table)


def _develop_stage(recipe: Recipe) -> tuple[Any, ...]:
    """The settings a kept frame was developed with: changing one develops it again."""
    develop = recipe.develop.model_dump(exclude={"exposure", "capture_sharpening", "capture_radius"})
    return (
        develop,
        recipe.camera.model_dump(),
        recipe.output.fill_dropped,
        recipe.output.frame_rate,
        recipe.output.frame_rate_drift_ms,
    )


class PreviewSession:
    """A clip (RAW or Direct Log) open for previews with a Recipe. Nothing is written beside the clip;
    the motion plan goes into a folder under `cache_dir() / "preview"`, removed on close."""

    def __init__(self, clip_path: Path, recipe: Recipe, settings: PreviewSettings | None = None) -> None:
        self.clip_path, self.recipe = clip_path, recipe
        self.settings = settings or PreviewSettings()
        self._facts, self._video = read_source(clip_path, recipe, audio=False)
        self._auto: dict[str, float] = {}  # measured auto exposure (stops) by develop settings
        self._job = self._job_for(recipe)
        self.frame_count = self._facts.frame_count
        self.size = self._preview_size(self._facts, self._video is None)
        self._clip: Clip | None = None
        self._read_clip: Clip | None = None
        self._reader: ThreadPoolExecutor | None = None
        self._read_ahead: tuple[int, bool, Future[tuple[RawFrame, dict[str, float]]]] | None = None
        self._developer: Developer | None = None
        self._draft_developer: Developer | None = None
        self._video_frames: VideoFrames | None = None
        self._pipeline: FastPipeline | None = None
        self.fast_timings: dict[str, float] = {}
        self.backend = PreviewBackend(enabled=self.settings.gpu and gpu_enabled())
        self._fast_kept: tuple[int, int, Image] | None = None
        self._linear_cache = FrameCache[Image](self.settings.cache_mib * 1024**2 * 3 // 4)
        self._jpeg_cache = FrameCache[Refined](self.settings.cache_mib * 1024**2 // 4)
        self._kept: tuple[int, Image] | None = None  # (frame index, scene-linear AP1 at preview size)
        self._table = display_table(33 if self.settings.fast_open else 65)
        self._finisher: tuple[int, Finisher] | None = None
        self._folder: Path | None = None
        self._plans: dict[str, tuple[Path | None, list[str]]] = {}  # (plan, notes) by motion settings
        self._planning = threading.Lock()  # refined frames may be made on another thread

    @property
    def pipeline(self) -> FastPipeline:
        if self._pipeline is None:
            self._pipeline = FastPipeline(self)
        return self._pipeline

    def cancel_pending(self) -> None:
        if self._pipeline is not None:
            self._pipeline.cancel()

    def close_pipeline(self) -> None:
        if self._pipeline is not None:
            self._pipeline.close()
            self._pipeline = None

    def fork_fast(self) -> PreviewSession:
        """A fast-only lane sharing bounded caches, with independent mutable working storage."""
        lane = copy.copy(self)
        lane._pipeline = None
        lane.backend = PreviewBackend(enabled=self.settings.gpu and gpu_enabled())
        lane.backend.cancelled = self.backend.cancelled
        lane._clip = lane._read_clip = lane._developer = lane._draft_developer = None
        lane._video_frames = None
        lane._reader = lane._read_ahead = None
        lane._fast_kept = lane._kept = lane._finisher = None
        lane._folder = None
        lane._plans = {}
        lane._planning = threading.Lock()
        return lane

    def adopt_fast(self, snapshot: PreviewSession) -> None:
        if self._linear_cache is not snapshot._linear_cache:
            self._developer = self._draft_developer = None
            self._fast_kept = None
        self.recipe, self._job, self._table = snapshot.recipe, snapshot._job, snapshot._table
        self._facts, self._video, self.frame_count = snapshot._facts, snapshot._video, snapshot.frame_count
        self.size = snapshot.size
        self._linear_cache, self._jpeg_cache = snapshot._linear_cache, snapshot._jpeg_cache
        self._auto = snapshot._auto

    def _clear_jpegs(self) -> None:
        # Detach the generation: cancelled lanes may finish a stage, but cannot repopulate it.
        old = self._jpeg_cache
        self._jpeg_cache = FrameCache[Refined](old.limit)
        old.retire()

    @property
    def full_size(self) -> tuple[int, int]:
        """The clip's frame size (RAW: photosites), as an export develops it."""
        return self._facts.width, self._facts.height

    @property
    def supports_source(self) -> bool:
        return self._video is None

    @property
    def fps(self) -> float:
        """The measured frame rate."""
        return self._facts.measured_fps

    @property
    def rate(self) -> float:
        """The labelled frame rate (24, 25, 30 and so on), which exports and the preview's sound use."""
        return float(self._facts.rate)

    @property
    def label(self) -> str | None:
        """What the preview needs said about it: nothing, now that every preview is SDR."""
        return None

    def frame(self, index: int, width: int | None = None) -> bytes:
        """Frame `index` developed at preview size (kept for `refinish` and `update`), finished, as JPEG."""
        if len(self._table) != 65**3:
            self.set_recipe(self.recipe)
        self.keep(index)
        return self.refinish(width)

    def keep(self, index: int) -> None:
        """Develop frame `index` at preview size and keep it, unless it is the kept one already."""
        if not 0 <= index < self.frame_count:
            raise IndexError(f"frame {index} is outside the clip's {self.frame_count} frames")
        if self._kept is None or self._kept[0] != index:
            self._kept, self._finisher = (index, self._develop(index)), None

    def refinish(self, width: int | None = None) -> bytes:
        """The kept frame finished again: exposure and the display rendering; no stabilisation."""
        encoded = self._encoded()
        if width is not None and encoded.shape[1] > width:
            encoded = _resized(encoded, (width, max(1, round(encoded.shape[0] * width / encoded.shape[1]))))
        return self._encode(display_bgr(encoded, self._table))

    def fast(
        self,
        index: int,
        width: int = 1280,
        *,
        prefetch: bool = False,
        drag: bool = False,
    ) -> Refined:
        """A small draft without sharpening or stabilisation.

        Cache linear frames so exposure changes only finish them again.
        """
        return self.draft_encoder(index, width, prefetch=prefetch, drag=drag)()

    def draft_encoder(
        self,
        index: int,
        width: int = 1280,
        *,
        prefetch: bool = False,
        drag: bool = False,
        display: DisplayFrames | None = None,
    ) -> Callable[[], Refined]:
        """Develop a draft (as `fast`) now; return the work another thread can do: finishing on the CPU
        (a device finishes here, on its owner's thread) and JPEG encoding, or the copy to `display`."""
        if not 0 <= index < self.frame_count:
            raise IndexError(f"frame {index} is outside the clip's {self.frame_count} frames")
        started = time.perf_counter()
        self.backend.check_cancelled()
        self.backend.begin_frame()
        width = min(width, self.settings.width)
        quality = min(65, self.settings.quality) if drag else self.settings.quality
        key = (index, width, quality)
        cache = self._jpeg_cache
        cached = cache.get(key) if display is None else None
        if cached is not None:
            self.fast_timings = {"cache_hit": 1.0, "total_ms": (time.perf_counter() - started) * 1000}
            return lambda: cached
        linear, reader_timings, decoded_ms, developed_ms = self._draft_linear(index, width, prefetch)
        table, stops = self._table, self._job.exposure_stops
        finishing = time.perf_counter()
        after_bgr: npt.NDArray[np.uint8] | None = None
        if self.backend.device is not None:  # the device's queue belongs to this thread
            after_bgr = self.backend.finish(linear, table, stops)
        finished_ms = (time.perf_counter() - finishing) * 1000
        timings = (
            self.backend.timings
            | reader_timings
            | {"decode_ms": decoded_ms, "develop_ms": developed_ms, "finish_ms": finished_ms}
        )
        self.fast_timings = timings
        check = self.backend.check_cancelled
        check()

        def encode() -> Refined:
            nonlocal after_bgr
            check()
            if after_bgr is None:  # the CPU's finishing is independent of the developing thread
                cpu = time.perf_counter()
                after_bgr = _raw.apply_preview(linear, table, stops)
                timings["finish_ms"] += (time.perf_counter() - cpu) * 1000
            check()
            encoding = time.perf_counter()
            if display is not None:
                lease = display.write(after_bgr, check)
                timings["display_copy_ms"] = (time.perf_counter() - encoding) * 1000
                timings["total_ms"] = (time.perf_counter() - started) * 1000

                def release() -> None:
                    display.release(lease)

                return Refined(b"", size=(linear.shape[1], linear.shape[0]), display=lease, release=release)
            jpeg = self._encode(after_bgr, quality=quality)
            ended = time.perf_counter()
            timings["jpeg_ms"] = (ended - encoding) * 1000
            timings["total_ms"] = (ended - started) * 1000
            frame = Refined(jpeg, size=(linear.shape[1], linear.shape[0]))
            check()
            cache.put(key, frame, len(jpeg))
            return frame

        return encode

    def still(self, index: int, width: int) -> npt.NDArray[np.uint8]:
        """Frame `index` as a draft (as `fast`) finished on the CPU, as 8-bit BGR at most `width` wide:
        a still to save, such as a file manager's thumbnail."""
        if not 0 <= index < self.frame_count:
            raise IndexError(f"frame {index} is outside the clip's {self.frame_count} frames")
        linear = self._draft_linear(index, min(width, self.settings.width), prefetch=False)[0]
        return np.asarray(_raw.apply_preview(linear, self._table, self._job.exposure_stops))

    def _draft_linear(
        self, index: int, width: int, prefetch: bool
    ) -> tuple[Image, dict[str, float], float, float]:
        """The draft of frame `index` in scene-linear AP1, turned for display and at most `width` wide,
        developed now unless it is kept; with the reader's timings and the decode and develop times (ms)."""
        reader_timings: dict[str, float] = {}
        decoded_ms = developed_ms = 0.0
        kept = self._linear_cache.get((index, width))
        if kept is not None:
            self._fast_kept = (index, width, kept)
        if self._fast_kept is None or self._fast_kept[:2] != (index, width):
            if self._video is None:
                developer, data, reader_timings, decoded_ms = self._read_raw(index, prefetch, reduced=True)
                step = 4 if data.shape[1] // 2 >= 2 * width else 2
                developing = time.perf_counter()
                linear = self.backend.develop(developer, data, step, width)
                developed_ms = (time.perf_counter() - developing) * 1000
            else:
                developing = time.perf_counter()
                linear = self._develop_video(index, width)
                developed_ms = (time.perf_counter() - developing) * 1000
            if self._job.rotation:
                linear = np.ascontiguousarray(np.rot90(linear, -self._job.rotation // 90))
            if linear.shape[1] > width:
                linear = _resized(linear, (width, max(1, round(linear.shape[0] * width / linear.shape[1]))))
            self.backend.check_cancelled()
            self._fast_kept = (index, width, linear)
            self._linear_cache.put((index, width), linear, linear.nbytes)
        return self._fast_kept[2], reader_timings, decoded_ms, developed_ms

    def _read_raw(
        self, index: int, prefetch: bool, *, reduced: bool = False
    ) -> tuple[Developer, npt.NDArray[np.uint16], dict[str, float], float]:
        source = self._job.sources[index] if self._job.sources is not None else index
        clip, developer = self._raw(source)
        reduced = reduced and self._job.crop is None
        started = time.perf_counter()
        if self._read_ahead is not None and self._read_ahead[:2] == (source, reduced):
            task = self._read_ahead[2]
            self._read_ahead = None
            raw_frame, timings = task.result()
            data = raw_frame.data
        else:
            data = (clip.draft_frame(source) if reduced else clip.raw_frame(source)).data
            timings = clip.timings
        self.backend.check_cancelled()
        decoded_ms = (time.perf_counter() - started) * 1000
        if prefetch and index + 1 < self.frame_count:
            following = self._job.sources[index + 1] if self._job.sources is not None else index + 1
            self._prefetch(following, reduced=reduced)
        if self._job.crop is not None:
            data = crop_data(data, CropBox(*self._job.crop))
        if data.shape != developer.black.shape:
            if self._draft_developer is None:
                # Preserve the calibrated sensor area and each Bayer channel while reducing
                # its black/gain planes. This is a draft only, never mounted or exported.
                small = copy.copy(developer)
                small.black = np.empty(data.shape, np.float32)
                small.gain = np.empty(data.shape, np.float32)
                for y in (0, 1):
                    for x in (0, 1):
                        size = (data.shape[1] // 2, data.shape[0] // 2)
                        small.black[y::2, x::2] = cv2.resize(
                            developer.black[y::2, x::2], size, interpolation=cv2.INTER_AREA
                        )
                        small.gain[y::2, x::2] = cv2.resize(
                            developer.gain[y::2, x::2], size, interpolation=cv2.INTER_AREA
                        )
                small._half = None
                self._draft_developer = small
            developer = self._draft_developer
        return developer, data, timings, decoded_ms

    def source_encoder(
        self, index: int, *, prefetch: bool = False, display: DisplayFrames | None = None
    ) -> Callable[[], Refined]:
        """Develop every photosite now; return the independent JPEG work for another thread."""
        if not self.supports_source:
            raise ValueError("source-resolution preview is currently available for RAW clips")
        if not 0 <= index < self.frame_count:
            raise IndexError(f"frame {index} is outside the clip's {self.frame_count} frames")
        started = time.perf_counter()
        self.backend.check_cancelled()
        key = ("source", index, self.settings.quality)
        cache = self._jpeg_cache
        cached = cache.get(key) if display is None else None
        if cached is not None:
            self.fast_timings = {"cache_hit": 1.0, "total_ms": (time.perf_counter() - started) * 1000}
            return lambda: cached
        self.backend.begin_frame()
        developer, raw, reader_timings, decoded_ms = self._read_raw(index, prefetch)
        developing = time.perf_counter()
        after = self.backend.preview_full(developer, raw, self._table, self._job.exposure_stops)
        if self._job.rotation:
            after = np.ascontiguousarray(np.rot90(after, -self._job.rotation // 90))
        self.backend.check_cancelled()
        timings = (
            self.backend.timings
            | reader_timings
            | {
                "decode_ms": decoded_ms,
                "develop_ms": (time.perf_counter() - developing) * 1000,
            }
        )
        self.fast_timings = timings

        def encode() -> Refined:
            self.backend.check_cancelled()
            encoding = time.perf_counter()
            if display is not None:
                lease = display.write(after, self.backend.check_cancelled)
                timings["display_copy_ms"] = (time.perf_counter() - encoding) * 1000
                timings["total_ms"] = (time.perf_counter() - started) * 1000

                def release() -> None:
                    display.release(lease)

                return Refined(
                    b"",
                    size=(after.shape[1], after.shape[0]),
                    display=lease,
                    release=release,
                )
            frame = Refined(self._encode(after), size=(after.shape[1], after.shape[0]))
            self.backend.check_cancelled()
            timings["jpeg_ms"] = (time.perf_counter() - encoding) * 1000
            timings["total_ms"] = (time.perf_counter() - started) * 1000
            cache.put(key, frame, len(frame.jpeg))
            return frame

        return encode

    @property
    def cache_bytes(self) -> int:
        """Retained cache payload bytes; the current working frame is separate."""
        return self.cache_usage()["bytes"]

    def cache_usage(self) -> dict[str, int]:
        linear, linear_limit, linear_entries = self._linear_cache.usage()
        jpeg, jpeg_limit, jpeg_entries = self._jpeg_cache.usage()
        return {
            "bytes": linear + jpeg,
            "linear_bytes": linear,
            "jpeg_bytes": jpeg,
            "limit_bytes": linear_limit + jpeg_limit,
            "entries": linear_entries + jpeg_entries,
        }

    def clear_cache(self) -> None:
        """Release cached payloads without cancelling playback or discarding the current picture.

        In-flight lanes keep their working frames and leases. Their retired cache generations
        reject late writes; subsequent requests capture the new, equally bounded caches.
        """
        linear, jpeg = self._linear_cache, self._jpeg_cache
        self._linear_cache = FrameCache[Image](linear.limit)
        self._jpeg_cache = FrameCache[Refined](jpeg.limit)
        linear.retire()
        jpeg.retire()

    def _prefetch(self, source: int, *, reduced: bool = False) -> None:
        """One independent RAW read/decompression overlaps the current device development and JPEG.

        There is never a queue of speculative reads. A seek may leave one old read to finish; it
        cannot delay the foreground reader or enqueue work all the way to the end of the clip.
        """
        if self._read_ahead is not None and not self._read_ahead[2].done():
            return
        if self._reader is None:
            self._read_clip = open_clip(self._job)
            self._reader = ThreadPoolExecutor(
                1, thread_name_prefix="preview-raw", initializer=background_priority
            )
        clip = self._read_clip
        assert clip is not None

        def read() -> tuple[RawFrame, dict[str, float]]:
            frame = clip.draft_frame(source) if reduced else clip.raw_frame(source)
            return frame, clip.timings

        self._read_ahead = (source, reduced, self._reader.submit(read))

    def linear16(self) -> tuple[bytes, tuple[int, int]]:
        """The kept frame at its exposure as 16-bit ACEScct RGB before the display table, little-endian,
        with its (width, height): what a page applying the table itself needs."""
        codes = codes16(self._encoded())
        return codes.astype("<u2").tobytes(), (codes.shape[1], codes.shape[0])

    @property
    def auto_pending(self) -> bool:
        """The Recipe's exposure is automatic and not measured yet: frames show 0 stops until it is."""
        key = self.auto_key(self.recipe)
        return key is not None and key not in self._auto

    def auto_stops(self, recipe: Recipe | None = None) -> float | None:
        """The measured automatic exposure for `recipe` (the session's by default), or None when its
        exposure is set or not measured yet."""
        key = self.auto_key(recipe or self.recipe)
        return self._auto.get(key) if key is not None else None

    def auto_measurer(
        self, *, cancelled: Callable[[], bool] | None = None
    ) -> tuple[str, Callable[[], float]] | None:
        """For an automatic exposure not measured yet: its key and a call that measures it as an export
        does (resolve_auto, seconds for a long clip; it may run on another thread). Give the result to
        set_auto."""
        key = self.auto_key(self.recipe)
        if key is None or key in self._auto:
            return None
        recipe, job, frames = self.recipe, self._job, self.frame_count

        def check(*args: object) -> None:
            if cancelled is not None and cancelled():
                raise CancelledError()

        def measure() -> float:
            check()
            # Interactive work takes priority over this full-quality, sampled measurement.
            exposure = resolve_auto(recipe, job, frames, 1, progress=check).recipe.develop.exposure
            check()
            return float(exposure) if exposure != "auto" else 0.0

        return key, measure

    def set_auto(self, key: str, stops: float) -> None:
        """The measured automatic exposure for the develop settings `key` (from auto_measurer)."""
        self._auto[key] = stops
        self._job = self._job_for(self.recipe)
        self._clear_jpegs()

    def update(self, recipe: Recipe) -> bytes:
        """The kept frame with a new Recipe: developed again only when a develop setting changed. A Recipe
        that cannot be previewed raises and leaves the session as it was."""
        self.set_recipe(recipe)
        return self.refinish()

    def set_recipe(self, recipe: Recipe, *, fast: bool = False) -> None:
        """update(recipe) without finishing the frame."""
        table = display_table(33 if fast else 65)
        developed = _develop_stage(self.recipe) != _develop_stage(recipe)
        if developed or self.recipe.develop.exposure != recipe.develop.exposure:
            self._clear_jpegs()
        if table is not self._table:
            self._clear_jpegs()
        if developed:  # white balance, dropped frames and a video's encoding are read with the clip's facts
            self._fast_kept = None
            old = self._linear_cache
            self._linear_cache = FrameCache[Image](old.limit)
            old.retire()
            self._facts, self._video = read_source(self.clip_path, recipe, audio=False)
            self.frame_count, self._developer = self._facts.frame_count, None
            self._draft_developer = None
            self.size = self._preview_size(self._facts, self._video is None)
        job = self._job_for(recipe)
        if (job.cfa_layout, job.cfa_pattern) != (self._job.cfa_layout, self._job.cfa_pattern):
            self._close_readers()  # frames are read differently: open the clip again
        self._job, self._table, self.recipe = job, table, recipe
        if developed and self._kept is not None:
            index = min(self._kept[0], self.frame_count - 1)
            self._kept, self._finisher = (None if fast else (index, self._develop(index))), None

    def refined(self, index: int) -> Refined:
        """Frame `index` at full quality (sharpening and stabilisation), at preview size."""
        return self.refiner(index)()

    def refiner(self, index: int) -> Callable[[], Refined]:
        """refined(index) as a call made with the session's present settings, which may run on another
        thread while the session goes on making drafts."""
        if not 0 <= index < self.frame_count:
            raise IndexError(f"frame {index} is outside the clip's {self.frame_count} frames")
        recipe, job, frames = self.recipe, self._job, self.frame_count
        size = self.size

        def refine() -> Refined:
            table = display_table()
            finish = partial(_preview_bgr, table=table, size=size)
            plan, notes = self._motion_plan(recipe)
            planned = dataclasses.replace(job, plan_path=plan)
            with contextlib.closing(iter_frames(planned, frames, 1, indices=[index], finish=finish)) as made:
                bgr: npt.NDArray[np.uint8] = next(made)
            return Refined(self._encode(bgr), notes, size=size)

        return refine

    def _close_readers(self) -> None:
        if self._reader is not None:
            self._reader.shutdown(wait=True, cancel_futures=True)
            self._reader = None
        self._read_ahead = None
        if self._read_clip is not None:
            self._read_clip.close()
            self._read_clip = None
        if self._clip is not None:
            self._clip.close()
            self._clip, self._developer, self._draft_developer = None, None, None

    def close(self) -> None:
        self.close_pipeline()
        self._close_readers()
        if self._video_frames is not None:
            self._video_frames.close()
            self._video_frames = None
        self._linear_cache = FrameCache[Image](self._linear_cache.limit)
        self._jpeg_cache = FrameCache[Refined](self._jpeg_cache.limit)
        self._fast_kept = None
        self.backend.close()
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)
            self._folder = None
        self._plans.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def _job_for(self, recipe: Recipe) -> FrameJob:
        """The export's job, always rendering to ACEScct (what the display table takes), with an automatic
        exposure as measured once it has been."""
        job = source_job(self.clip_path, recipe, self._facts, self._video)
        job = dataclasses.replace(
            job,
            target="acescct",
            float_output=False,
            quad_bayer_remosaic=self.settings.quad_bayer_remosaic,
        )
        key = self.auto_key(recipe)
        if key is not None and key in self._auto:
            job = dataclasses.replace(job, exposure_stops=self._auto[key])
        return job

    def auto_key(self, recipe: Recipe | None = None) -> str | None:
        """What an automatic exposure's measurement depends on (the develop settings), or None when the
        exposure is set."""
        recipe = recipe or self.recipe
        if recipe.develop.exposure != "auto":
            return None
        settings = json.dumps(_develop_stage(recipe), sort_keys=True, default=str)
        return hashlib.sha256(settings.encode()).hexdigest()

    def _encoded(self) -> npt.NDArray[np.float32]:
        """The kept frame at the present exposure as ACEScct (see preview/finish.py)."""
        if self._kept is None:
            raise RuntimeError("no frame is kept yet: ask for one with frame(index)")
        linear, job = self._kept[1], self._job
        if self._finisher is None or self._finisher[0] != job.rotation:  # a new kept frame clears it
            self._finisher = (job.rotation, Finisher(linear, job.rotation))
        return self._finisher[1].encoded(job.exposure_stops)

    def _preview_size(self, facts: ClipFacts, raw: bool) -> tuple[int, int]:
        """(width, height) as shown: RAW at half size, then at most `settings.width` wide."""
        width, height = (facts.width // 2, facts.height // 2) if raw else (facts.width, facts.height)
        if width <= self.settings.width:
            return width, height
        return self.settings.width, max(1, round(height * self.settings.width / width))

    def _develop(self, index: int) -> Image:
        """Frame `index` in scene-linear AP1 at preview size, in recorded orientation (a video's display
        turn is made when finishing)."""
        if self._video is None:
            source = self._job.sources[index] if self._job.sources is not None else index
            clip, developer = self._raw(source)
            data = clip.raw_frame(source).data
            if self._job.crop is not None:
                data = crop_data(data, CropBox(*self._job.crop))
            size = self.size[::-1] if self._job.rotation % 180 else self.size
            return _resized(developer.develop_half(data), size)
        return self._develop_video(index, self.settings.width)

    def _develop_video(self, index: int, width: int) -> Image:
        assert self._video is not None
        info, encoding = self._video
        # Decode/convert at the requested draft size, not the full 4K/8K image size.
        scale = min(1.0, width / (info.height if info.rotation % 180 else info.width))
        size = (max(1, round(info.width * scale)), max(1, round(info.height * scale)))
        if self._video_frames is None or self._video_frames.size != size:
            if self._video_frames is not None:
                self._video_frames.close()
            self._video_frames = VideoFrames(self.clip_path, info, size, gpu=self.settings.gpu)
        source = self._job.sources[index] if self._job.sources is not None else index
        rgb = self._video_frames.frame(source, self.backend.check_cancelled)
        return video_linear(dataclasses.replace(self._job, input_encoding=encoding), rgb)

    def _raw(self, index: int = 0) -> tuple[Clip, Developer]:
        if self._clip is None:
            self._clip = open_clip(self._job)
        if self._developer is None:
            job, clip = self._job, self._clip
            container = clip.metadata
            if job.colour_matrix is not None:
                container = with_colour_matrix(container, job.colour_matrix)
            self._developer = Developer(
                container,
                first_metadata(job, clip),
                job.neutral,
                job.highlight_recovery,
                job.lens_shading,
                shading_mode=job.lens_shading_mode,
                black_level=job.black_level,
                white_level=job.white_level,
            )
        if self._job.dynamic_levels:
            updated = self._developer.with_levels(self._clip.frame_metadata(index))
            if updated is not self._developer:
                self._developer = updated
                self._draft_developer = None
        return self._clip, self._developer

    def _motion_plan(self, recipe: Recipe) -> tuple[Path | None, list[str]]:
        """The stabilisation plan for `recipe` and the motion step's notes, planned once per session for
        each motion setting."""
        key = motion_key(self.clip_path, recipe)
        with self._planning:
            if key not in self._plans:
                if self._folder is None:
                    parent = cache_dir() / "preview"
                    parent.mkdir(parents=True, exist_ok=True)
                    self._folder = Path(tempfile.mkdtemp(prefix="session-", dir=parent))
                staging = Path(tempfile.mkdtemp(prefix="motion-", dir=self._folder))
                info = self._video[0] if self._video is not None else None
                try:
                    outcome, _ = plan_motion(
                        self.clip_path, recipe, info, find_ffmpeg()[0], min(default_workers(), 4), staging
                    )
                except BaseException:  # nothing kept: the same settings plan again next time
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
                self._plans[key] = (outcome.plan_path, list(outcome.notes))
            return self._plans[key]

    def _encode(self, bgr: npt.NDArray[np.uint8], *, quality: int | None = None) -> bytes:
        """8-bit BGR as a JPEG."""
        ok, encoded = cv2.imencode(
            ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.settings.quality if quality is None else quality]
        )
        if not ok:
            raise ValueError(f"{self.clip_path.name}: the preview could not be encoded as JPEG")
        return encoded.tobytes()
