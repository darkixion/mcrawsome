"""Presentation times without decoding pictures, cached by the source file's identity."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome.io.movie_metadata import MAX_SAMPLES, MovieMetadataError, read_movie_metadata


class VideoTimingError(ValueError):
    pass


def presentation_times(ffprobe: Path, path: Path) -> npt.NDArray[np.int64]:
    stamp = path.stat()
    return _times(str(ffprobe), path, stamp.st_size, stamp.st_mtime_ns)


@lru_cache(maxsize=16)
def _times(ffprobe: str, path: Path, size: int, modified: int) -> npt.NDArray[np.int64]:
    try:
        times = read_movie_metadata(path).times_ns
    except MovieMetadataError:
        # MKV and fragmented MP4 have no static sample table. Demux packet headers,
        # never decode video, and bound memory even for a malformed input.
        points = []
        with subprocess.Popen(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_packets",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        ) as process:  # fmt: skip
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    value = line.strip().split(",")[0]
                    if not value:
                        continue
                    try:
                        points.append(round(float(value) * 1e9))
                    except (ValueError, OverflowError):
                        raise VideoTimingError("Video packets have no valid presentation times") from None
                    if len(points) > MAX_SAMPLES:
                        raise VideoTimingError("Video has more than two million samples")
                process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait()
            if process.returncode != 0:
                raise VideoTimingError("Cannot read video packet times") from None
        times = np.asarray(sorted(points), dtype=np.int64)
        if len(times):
            times -= times[0]
    if not len(times) or (len(times) > 1 and np.any(np.diff(times) <= 0)):
        raise VideoTimingError("Video presentation times must increase")
    times.flags.writeable = False
    return times
