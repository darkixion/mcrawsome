"""A clip's sound as peaks across its timeline, for the trim bar's waveform.

The sound is what the preview plays: a RAW clip's WAV as its mount serves it (mount.audio.WaveView, in step
with the frames shown), or a video's extracted WAV (preview.audio). It is cut into POINTS equal parts, each
given its loudest sample as a fraction of full scale, and cached by the clip's fingerprint.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import is_video_input
from mcrawsome.mount.audio import WaveView
from mcrawsome.output.chunks import InputFile
from mcrawsome.output.export import clip_sources
from mcrawsome.output.ffmpeg import frame_rate
from mcrawsome.paths import cache_dir
from mcrawsome.preview.audio import clip_audio

POINTS = 400  # parts across the clip
_VERSION = 1

Reader = Callable[[int, int], npt.NDArray[np.int16]]  # samples from the first, as (count, channels)


@contextlib.contextmanager
def _sound(path: Path) -> Iterator[tuple[int, Reader] | None]:
    """The clip's sound, as its length in samples and a reader of them; None when it has none."""
    if is_video_input(path):
        try:
            extracted = clip_audio(path)
        except McError as exc:
            if exc.code != "input-missing":
                raise
            yield None
            return
        with wave.open(str(extracted), "rb") as source:
            channels = source.getnchannels()

            def read_wav(first: int, count: int) -> npt.NDArray[np.int16]:
                source.setpos(first)
                return np.frombuffer(source.readframes(count), "<i2").reshape(-1, channels)

            yield source.getnframes(), read_wav
        return
    with path.open("rb") as stream, Clip(path, metadata_only=True, quad_bayer_remosaic=False) as clip:
        if not clip.audio_chunk_count or not clip.metadata.audio_sample_rate:
            yield None
            return
        rate = float(frame_rate(clip.fps))  # as the preview's audio (server.routes.audio)
        view = WaveView(clip, stream.fileno(), len(clip_sources(clip, True)), rate, rate / clip.fps)
        head, stride = len(view.header), view.channels * 2

        def read_view(first: int, count: int) -> npt.NDArray[np.int16]:
            pcm = view.read(head + first * stride, count * stride)
            return np.frombuffer(pcm, "<i2").reshape(-1, view.channels)

        yield view.length, read_view


def _measure(path: Path) -> list[float]:
    with _sound(path) as sound:
        if sound is None:
            return []
        length, read = sound
        peaks = []
        for part in range(POINTS):
            first, stop = part * length // POINTS, (part + 1) * length // POINTS
            loudest = (
                int(np.abs(read(first, stop - first).astype(np.int32)).max(initial=0)) if stop > first else 0
            )
            peaks.append(round(loudest / 32768, 4))
        return peaks


def waveform(path: Path) -> list[float]:
    """The loudest sample in each of POINTS equal parts of the clip, from its first frame to its last, as a
    fraction of full scale; none for a clip without sound."""
    key = f"{InputFile.of(path).model_dump_json()}|{POINTS}|{_VERSION}"
    cached = cache_dir() / "waveforms" / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"
    with contextlib.suppress(OSError, ValueError):
        peaks: list[float] = json.loads(cached.read_text())
        return peaks
    peaks = _measure(path)
    cached.parent.mkdir(parents=True, exist_ok=True)
    part = cached.with_name(f".{cached.name}.{secrets.token_hex(4)}.part")
    try:
        part.write_text(json.dumps(peaks))
        part.replace(cached)  # whole or not at all, even with two requests at once
    finally:
        part.unlink(missing_ok=True)
    return peaks
