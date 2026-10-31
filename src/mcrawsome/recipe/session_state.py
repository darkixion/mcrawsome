"""Complete local recipe overrides restored from sessions, independent of folder defaults."""

from __future__ import annotations

import hashlib
from pathlib import Path

from mcrawsome.output.sidecar import quick_hash
from mcrawsome.paths import state_dir


def session_path(clip: Path, first_16mib: str | None = None) -> Path:
    fingerprint = first_16mib if first_16mib is not None else quick_hash(clip)
    key = hashlib.sha256(f"{clip.resolve()}|{clip.stat().st_size}|{fingerprint}".encode()).hexdigest()[:20]
    return state_dir() / "session-recipes" / f"{key}.toml"


def session_override(clip: Path, first_16mib: str | None = None) -> Path | None:
    if not (state_dir() / "session-recipes").is_dir() or not clip.is_file():
        return None
    path = session_path(clip, first_16mib)
    return path if path.is_file() else None
