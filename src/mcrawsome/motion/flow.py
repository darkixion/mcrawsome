"""Image motion between frames: DIS optical flow and a robust similarity fit (shift and roll).

Port of the motion measurement in the prototype's stabilise.py. DIS is used because Farneback flow
under-reads real motion by 20 to 35% on these clips.
"""

from __future__ import annotations

import atexit
import contextlib
import subprocess
import threading
from collections.abc import Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.io.clip import Clip, close_all
from mcrawsome.pipeline.pool import bounded_imap, worker_pool

FLOW_WIDTH = 480
Grey = npt.NDArray[np.uint8]


@dataclass(frozen=True)
class MotionTrack:
    """Measured motion from frame `first[k]` to `first[k] + 1`, in full-resolution pixels and radians."""

    first: npt.NDArray[np.int64]
    dx: npt.NDArray[np.float64]
    dy: npt.NDArray[np.float64]
    roll: npt.NDArray[np.float64]


def _to_grey(values: npt.NDArray[np.float32]) -> Grey:
    height = round(FLOW_WIDTH * values.shape[0] / values.shape[1])
    small = cv2.resize(values, (FLOW_WIDTH, height), interpolation=cv2.INTER_AREA)
    small = np.sqrt(np.maximum(small, 0.0))
    small = small / (np.percentile(small, 99.5) + 1e-6)
    return np.asarray(np.clip(small * 255, 0, 255), dtype=np.uint8)


def motion_image_raw(raw: npt.NDArray[np.uint16], cfa: str, black: float) -> Grey:
    """The green photosites of a raw frame, black-subtracted and square-rooted, at FLOW_WIDTH."""
    gy, gx = (0, 0) if cfa[0] == "g" else (0, 1)
    # Only the green sites enter motion estimation. Preserve the subtraction and averaging order
    # without converting the unused red/blue sites or keeping full-sensor float temporaries.
    green = raw[gy::2, gx::2].astype(np.float32)
    green -= np.float32(black)
    green += raw[gy + 1 :: 2, 1 - gx :: 2].astype(np.float32) - np.float32(black)
    green *= np.float32(0.5)
    return _to_grey(green)


def motion_image_video(rgb: npt.NDArray[np.float32]) -> Grey:
    """Luma of a decoded (display-encoded) frame at FLOW_WIDTH."""
    luma = rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    return _to_grey(np.asarray(luma**2, dtype=np.float32))  # undo the square root's effect on encoded values


def frame_motion(
    prev: Grey, nxt: Grey, full_width: int, rows: tuple[float, float] = (0.0, 1.0)
) -> tuple[float, float, float]:
    """(dx, dy, roll) from `prev` to `nxt`: shift in full-resolution pixels, roll in radians."""
    return _frame_motions(prev, nxt, full_width, (rows,))[0]


def _frame_motions(
    prev: Grey,
    nxt: Grey,
    full_width: int,
    strips: Sequence[tuple[float, float]],
    *,
    estimator: cv2.DISOpticalFlow | None = None,
) -> list[tuple[float, float, float]]:
    """Measure the dense flow once, then fit each strip independently."""
    if not strips:
        return []
    dis = estimator if estimator is not None else cv2.DISOpticalFlow.create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    flow = np.asarray(dis.calc(prev, nxt, np.zeros((*prev.shape, 2), np.float32)), dtype=np.float32)
    return [_fit_motion(flow, full_width, rows) for rows in strips]


def _fit_motion(
    flow: npt.NDArray[np.float32], full_width: int, rows: tuple[float, float]
) -> tuple[float, float, float]:
    height, width = flow.shape[:2]
    r0, r1 = int(height * rows[0]), int(height * rows[1])
    yy, xx = np.mgrid[r0:r1:6, 0:width:6].astype(np.float64)
    xc, yc = (xx - width / 2).ravel(), (yy - height / 2).ravel()
    u, v = flow[r0:r1:6, ::6, 0].ravel(), flow[r0:r1:6, ::6, 1].ravel()
    ones, zeros = np.ones_like(xc), np.zeros_like(xc)
    # u = tx - roll * y ; v = ty + roll * x (small rotation), by iteratively reweighted least squares
    a = np.block(
        [[ones[:, None], zeros[:, None], -yc[:, None]], [zeros[:, None], ones[:, None], xc[:, None]]]
    )
    b = np.concatenate([u, v]).astype(np.float64)
    weights = np.ones_like(b)
    solution = np.zeros(3)
    for _ in range(5):
        solution = np.linalg.lstsq(a * weights[:, None], b * weights, rcond=None)[0]
        weights = 1.0 / np.maximum(np.abs(a @ solution - b), 1.0)
    scale = FLOW_WIDTH / full_width
    return float(solution[0] / scale), float(solution[1] / scale), float(solution[2])


_clips: dict[Path, Clip] = {}


@atexit.register
def _close_clips() -> None:
    """Close the clips kept open for the process (a worker's, or the caller's when run in-process)."""
    clips = list(_clips.values())
    _clips.clear()
    close_all(clips)


def _raw_motion_image(clip_path: Path, index: int) -> Grey:
    if clip_path not in _clips:
        _clips[clip_path] = Clip(clip_path)
    clip = _clips[clip_path]
    frame = clip.raw_frame(index)
    return motion_image_raw(
        frame.data, clip.metadata.sensor_arrangement, float(np.mean(frame.metadata.black_level))
    )


Strips = Sequence[tuple[float, float]]
WHOLE: Strips = ((0.0, 1.0),)


def measure_motion_raw(clip_path: Path, indices: Sequence[int], workers: int) -> MotionTrack:
    """Motion between consecutive frames among `indices` of a RAW clip (decoding in the pool)."""
    from mcrawsome.motion.flow_cache import measured

    return measured(clip_path, indices, lambda: measure_strips_raw(clip_path, indices, workers, WHOLE)[0])


def measure_strips_raw(
    clip_path: Path,
    indices: Sequence[int],
    workers: int,
    strips: Strips,
    *,
    stop: threading.Event | None = None,
) -> list[MotionTrack]:
    """One track per strip of rows (fractions of the height, top first), each frame decoded once."""
    with Clip(clip_path) as clip:
        full_width = clip.frame_metadata(0).width
    with (
        worker_pool(workers) as pool,
        contextlib.closing(
            bounded_imap(pool, partial(_raw_motion_image, clip_path), list(indices), window=workers * 2)
        ) as images,
    ):
        return _tracks(list(indices), images, full_width, strips, stop=stop)


def _tracks(
    indices: list[int],
    images: object,
    full_width: int,
    strips: Strips,
    *,
    stop: threading.Event | None = None,
) -> list[MotionTrack]:
    estimator = cv2.DISOpticalFlow.create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    first: list[int] = []
    values: list[list[tuple[float, float, float]]] = [[] for _ in strips]
    previous: tuple[int, Grey] | None = None
    for index, image in zip(indices, images, strict=True):  # type: ignore[call-overload]
        if stop is not None and stop.is_set():
            raise CancelledError
        if previous is not None and previous[0] + 1 == index:
            first.append(previous[0])
            for motion, found in zip(
                _frame_motions(previous[1], image, full_width, strips, estimator=estimator),
                values,
                strict=True,
            ):
                found.append(motion)
        previous = (index, image)
    tracks = []
    for found in values:
        data = np.asarray(found, dtype=np.float64).reshape(-1, 3)
        tracks.append(MotionTrack(np.asarray(first, dtype=np.int64), data[:, 0], data[:, 1], data[:, 2]))
    return tracks


def measure_motion_video(
    ffmpeg: Path, video: Path, indices: Sequence[int], width: int, height: int
) -> MotionTrack:
    """Motion between consecutive frames among `indices` of a video, decoded small and grey by FFmpeg."""
    return measure_strips_video(ffmpeg, video, indices, width, height, WHOLE)[0]


def measure_strips_video(
    ffmpeg: Path,
    video: Path,
    indices: Sequence[int],
    width: int,
    height: int,
    strips: Strips,
    *,
    stop: threading.Event | None = None,
) -> list[MotionTrack]:
    """One track per strip of rows of a video, as `measure_strips_raw`."""
    small_h = round(FLOW_WIDTH * height / width / 2) * 2
    size = FLOW_WIDTH * small_h
    wanted = set(indices)
    proc = subprocess.Popen(
        [str(ffmpeg), "-nostdin", "-v", "error", "-noautorotate", "-i", str(video), "-map", "0:v:0",
         "-vf", f"scale={FLOW_WIDTH}:{small_h}:flags=area,format=gray",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE,
    )  # fmt: skip
    assert proc.stdout is not None
    images: list[Grey] = []
    kept: list[int] = []
    try:
        for index in range(max(indices) + 1 if indices else 0):
            if stop is not None and stop.is_set():
                raise CancelledError
            data = proc.stdout.read(size)
            if len(data) < size:
                break
            if index in wanted:
                kept.append(index)
                images.append(np.frombuffer(data, np.uint8).reshape(small_h, FLOW_WIDTH))
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()
    return _tracks(kept, images, width, strips, stop=stop)
