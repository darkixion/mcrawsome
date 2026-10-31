"""Direct LOG analytics from indexed movie timestamps and recorded telemetry."""

import math
import threading
from pathlib import Path

from mcrawsome.analysis.capture_plot import CaptureAnalytics
from mcrawsome.analysis.diagnostics import _check, timing_report
from mcrawsome.io.movie_metadata import MovieMetadataError, read_movie_metadata


def measure_log_capture(path: Path, stop: threading.Event) -> CaptureAnalytics:
    _check(stop)
    before = path.stat()
    notes = [
        "LOG uses the original video file; no virtual RAW mount is needed.",
        "Bayer shading, sensor noise, RAW levels and per-frame neutral coefficients are shown only "
        "when recorded. They cannot be reconstructed from developed LOG pixels.",
    ]
    try:
        movie = read_movie_metadata(path)
        timing = timing_report(movie.times_ns, stop)
        error = None
        frames = len(movie.times_ns)
        notes.append(
            "Frame timing comes from every indexed video presentation timestamp, not "
            "a nominal FPS calculation. It is the movie clock, not the RAW sensor clock."
        )
    except (MovieMetadataError, ValueError) as exc:
        timing, error, frames = None, str(exc), 0
        notes.append(
            "This movie has no supported static MP4/MOV sample table; no video payload scan was run."
        )
    _check(stop)
    series = []
    from mcrawsome.analysis.capture_plot import _Graphs, shake_blur
    from mcrawsome.analysis.motion_insights import LOG_EXPOSURE_NOTE

    blur = shake_blur(path) if frames else None
    if blur is not None and error is None:
        graphs = _Graphs(len(blur))
        for index, value in enumerate(blur):
            graphs.add(index, {"blur": float(value)} if math.isfinite(value) else {})
        series = graphs.result()
        for item in series:
            for point in item.points:
                point.time_s = float(movie.times_ns[point.frame] - movie.times_ns[0]) / 1e9
        notes.append(LOG_EXPOSURE_NOTE + ".")
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Video changed while reading metadata")
    return CaptureAnalytics(
        frames=frames,
        sampled_frames=0,
        unreadable_frames=0,
        timing=timing,
        timing_error=error,
        series=series,
        notes=notes,
    )
