"""A clip's sound for the preview page: a WAV extracted once into the cache.

The WAV is the DNG export's: 16-bit PCM at the clip's rate, starting at the first frame, as long as the
frames at the labelled frame rate, and played that much faster or slower (tempo-aligned) so it stays in
step with frames shown at the labelled rate.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path

from mcrawsome.errors import McError
from mcrawsome.output.audio import aligned_track, write_wav
from mcrawsome.output.chunks import InputFile
from mcrawsome.output.export import read_source
from mcrawsome.paths import cache_dir
from mcrawsome.recipe.schema import Recipe


def clip_audio(clip_path: Path) -> Path:
    """The WAV of `clip_path`'s sound, made the first time it is asked for (keyed by the clip's
    fingerprint, so an edited or replaced clip gets a new one)."""
    fingerprint = InputFile.of(clip_path).model_dump_json()
    folder = cache_dir() / "audio"
    path = folder / f"{hashlib.sha256(fingerprint.encode()).hexdigest()[:32]}.wav"
    if path.exists():
        return path
    facts, _ = read_source(clip_path, Recipe())
    if facts.track is None:
        raise McError(f"{clip_path.name} has no sound", code="input-missing")
    seconds = facts.frame_count / float(facts.rate)
    track = aligned_track(facts.track, facts.first_frame_ns, seconds, facts.tempo)  # as the DNG export
    folder.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(suffix=".wav.part", dir=folder)
    os.close(handle)
    try:
        write_wav(Path(staged), track)
        os.replace(staged, path)  # whole or not at all, even with two requests at once
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged)
    return path
