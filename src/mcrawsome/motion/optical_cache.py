"""Bounded disposable feature tracks, independent of lens, sync and smoothing choices."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from zipfile import BadZipFile

import cv2
import numpy as np

from mcrawsome.io.atomic import write_atomic
from mcrawsome.io.clip import RAW_DECODE_VERSION, remosaic_enabled
from mcrawsome.motion.optical import VERSION, FeatureTracks
from mcrawsome.paths import cache_dir

MAX_BYTES = 128 << 20


def _identity(source: Path) -> str:
    stat = source.stat()
    values = [
        RAW_DECODE_VERSION,
        cv2.__version__,
        np.__version__,
        remosaic_enabled(),
        str(source.resolve()),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def measured(
    source: Path,
    frames: int,
    compute: Callable[[], FeatureTracks],
    check: Callable[[], None],
) -> FeatureTracks:
    check()
    key = f"{VERSION}-{frames}-{_identity(source)}"
    folder = cache_dir() / "optical-tracks"
    path = folder / f"{key}.npz"
    try:
        if path.stat().st_size <= MAX_BYTES:
            with np.load(path, allow_pickle=False) as data:
                track = FeatureTracks(
                    data["first"],
                    data["identity"],
                    data["before"],
                    data["after"],
                    int(data["width"]),
                    int(data["height"]),
                )
            n = len(track.first)
            if (
                0 < n <= 3_000_000
                and track.first.dtype == np.int64
                and track.identity.dtype == np.int64
                and track.identity.shape == (n,)
                and track.width == 480
                and 1 < track.height <= 1920
                and track.before.shape == track.after.shape == (n, 2)
                and np.all(np.diff(track.first) >= 0)
                and track.first.min() >= 0
                and track.first.max() < frames - 1
                and track.identity.min() >= 0
                and all(
                    np.isfinite(p).all() and (p >= 0).all() and (p < [track.width, track.height]).all()
                    for p in (track.before, track.after)
                )
            ):
                check()
                return track
    except (OSError, ValueError, KeyError, TypeError, EOFError, BadZipFile):
        pass
    track = compute()
    check()
    try:
        if key == f"{VERSION}-{frames}-{_identity(source)}":
            content = io.BytesIO()
            np.savez_compressed(
                content,
                first=track.first,
                identity=track.identity,
                before=track.before,
                after=track.after,
                width=track.width,
                height=track.height,
            )
            if content.tell() <= MAX_BYTES:
                folder.mkdir(parents=True, exist_ok=True)
                write_atomic(path, content.getvalue())
                for old in sorted(folder.glob("*.npz"), key=lambda p: p.stat().st_mtime_ns)[:-4]:
                    old.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    return track
