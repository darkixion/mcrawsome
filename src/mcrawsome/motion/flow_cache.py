"""Bounded, disposable RAW image-motion measurements; gyro and crop settings are fitted afresh."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from itertools import pairwise
from pathlib import Path

import cv2
import numpy as np

from mcrawsome.io import clip
from mcrawsome.io.atomic import write_atomic
from mcrawsome.io.clip import RAW_DECODE_VERSION
from mcrawsome.motion.flow import FLOW_WIDTH, MotionTrack
from mcrawsome.paths import cache_dir

LIMIT = 64
MAX_BYTES = 256 << 10
VERSION = 1  # Bump when motion-image preparation or optical-flow fitting changes.


def _identity(source: Path, indices: Sequence[int]) -> str:
    stat = source.stat()
    values = [
        VERSION, RAW_DECODE_VERSION, FLOW_WIDTH, cv2.__version__, np.__version__, clip.remosaic_enabled(),
        str(source.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
        stat.st_ctime_ns, list(indices),
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def measured(source: Path, indices: Sequence[int], compute: Callable[[], MotionTrack]) -> MotionTrack:
    key = _identity(source, indices)
    folder = cache_dir() / "motion-tracks"
    path = folder / f"{key}.json"
    expected = np.asarray([a for a, b in pairwise(indices) if b == a + 1], dtype=np.int64)
    try:
        if path.stat().st_size <= MAX_BYTES:
            data = json.loads(path.read_bytes())
            first = np.asarray(data["first"], dtype=np.int64)
            values = [np.asarray(data[name], dtype=np.float64) for name in ("dx", "dy", "roll")]
            if np.array_equal(first, expected) and all(
                value.shape == first.shape and np.isfinite(value).all() for value in values
            ):
                return MotionTrack(first, *values)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    track = compute()
    try:
        if key == _identity(source, indices):
            content = json.dumps(
                {name: getattr(track, name).tolist() for name in ("first", "dx", "dy", "roll")}
            )
            if len(content) <= MAX_BYTES:
                folder.mkdir(parents=True, exist_ok=True)
                write_atomic(path, content.encode())
                for old in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime_ns)[:-LIMIT]:
                    old.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass  # A missing or unwritable cache must never prevent gyro analysis.
    return track
