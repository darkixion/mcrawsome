"""The sidecar file written next to every output, for reproducibility."""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w

from mcrawsome import __version__
from mcrawsome.paths import NAME
from mcrawsome.recipe.schema import Recipe

# Hashes by path and file identity (device, inode, size, modification and change times): the library,
# sessions and recipe lookups ask again for the same unchanged clips many times a second.
_HASHES: dict[tuple[str, int, int, int, int, int], str] = {}
_HASHES_LOCK = threading.Lock()
_HASHES_KEPT = 4096


def _identity(path: Path) -> tuple[str, int, int, int, int, int]:
    stat = path.stat()
    return os.fspath(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def quick_hash(path: Path) -> str:
    """The sha256 of the file's first 16 MiB. An unchanged file is read once: the hash is remembered by its
    identity, and only when the file did not change while it was read."""
    before = _identity(path)
    with _HASHES_LOCK:
        known = _HASHES.get(before)
    if known is not None:
        return known
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(16 * 1024 * 1024))
    digest = h.hexdigest()
    if _identity(path) == before:
        with _HASHES_LOCK:
            if len(_HASHES) >= _HASHES_KEPT:
                del _HASHES[next(iter(_HASHES))]  # the oldest
            _HASHES[before] = digest
    return digest


def sidecar_path(output: Path) -> Path:
    """An export's settings record, NAME.mcrawsome.toml."""
    return output.with_name(f"{output.name}.{NAME}.toml")


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        if any(v is None for v in value):
            raise ValueError("a list in the sidecar holds None; dropping it would shift the other items")
        return [_without_none(v) for v in value]
    return value


def write_sidecar(
    output: Path,
    recipe: Recipe,
    measurements: dict[str, Any],
    inputs: list[Path],
    frames: dict[str, Any] | None = None,
) -> Path:
    """`frames`, for RAW exports: the source and output frame counts and the repeated output frames."""
    path = sidecar_path(output)
    document = {
        "mcrawsome_version": __version__,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "inputs": [{"path": str(p), "sha256_first_16mib": quick_hash(p)} for p in inputs],
        "recipe": recipe.model_dump(mode="json", exclude_none=True),
        "measurements": measurements,
        "frames": frames,
    }
    text = tomli_w.dumps(_without_none(document))
    part = path.with_name(path.name + ".part")
    try:
        part.write_text(text, encoding="utf-8")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return path
