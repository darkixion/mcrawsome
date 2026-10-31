"""Picture-measured exposure normalisation for mounted media: each recorded frame's gain, measured once.

The measurement is the deflicker's (develop/deflicker.py) at full strength: each frame's brightness (its
unclipped green photosites, or its recorded exposure when too few are usable) is brought to the moving
average over a window. A mount cannot read the whole clip as an editor asks for one frame, so the clip is
read once before it is mounted, by threads in this process (the decoder releases the interpreter while it
works), and the gains are kept in the cache by the recording's identity and the settings that change them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcrawsome.develop.crop import CropBox, crop_data
from mcrawsome.develop.deflicker import frame_brightness, normalising_gains, window_frames
from mcrawsome.develop.raw import levels
from mcrawsome.io.atomic import write_atomic
from mcrawsome.io.clip import Clip
from mcrawsome.paths import cache_dir
from mcrawsome.recipe.raw import RawCalibration

_VERSION = 1  # of the measurement: a change measures every clip again
_THREADS = 4


def _key(
    source: Path,
    stamp: tuple[int, int, int, int],
    settings: RawCalibration,
    crop: CropBox | None,
    quad_bayer_remosaic: bool,
) -> str:
    parts = [
        _VERSION, str(source), list(stamp), settings.black_level, settings.white_level, quad_bayer_remosaic,
        list(dataclasses.astuple(crop)) if crop is not None else None, settings.mount_normalise_seconds,
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]


def measure(clip: Clip, settings: RawCalibration, crop: CropBox | None) -> list[float]:
    """Every recorded frame's gain (one pass over the clip)."""
    settings = settings.serving(clip.frame_metadata(0))
    count, pattern = clip.frame_count, clip.metadata.sensor_arrangement
    local = threading.local()
    readers: list[Clip] = []
    lock = threading.Lock()

    def one(index: int) -> tuple[float | None, float]:
        if not hasattr(local, "clip"):
            local.clip = clip.independent_reader()
            with lock:
                readers.append(local.clip)
        raw = local.clip.raw_frame(index)
        data = crop_data(raw.data, crop) if crop is not None else raw.data
        blacks, white = levels(raw.metadata, settings.black_level, settings.white_level)
        return (
            frame_brightness(data, pattern, blacks, white),
            raw.metadata.iso * raw.metadata.exposure_time_ns,
        )

    try:
        with ThreadPoolExecutor(_THREADS, thread_name_prefix="exposure") as pool:
            found = list(pool.map(one, range(count)))
    finally:
        for reader in readers:
            reader.close()
    window = window_frames(settings.mount_normalise_seconds, clip.fps)
    return normalising_gains([b for b, _ in found], [e for _, e in found], window, 1.0)


def picture_gains(
    clip: Clip, source: Path, stamp: tuple[int, int, int, int], settings: RawCalibration, crop: CropBox | None
) -> list[float]:
    """The gains from the cache, measured first when they are not there."""
    settings = settings.serving(clip.frame_metadata(0))
    key = _key(source, stamp, settings, crop, clip.quad_bayer_remosaic)
    path = cache_dir() / "exposure" / f"{key}.json"
    try:
        gains = json.loads(path.read_text())["gains"]
        if isinstance(gains, list) and len(gains) == clip.frame_count:
            return [float(g) for g in gains]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    gains = measure(clip, settings, crop)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps({"source": str(source), "gains": gains}).encode())
    except OSError:
        pass  # measured again next time
    return gains
