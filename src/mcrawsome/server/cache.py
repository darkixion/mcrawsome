"""Inventory and reclaim only caches whose consumers retain independent data snapshots."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mcrawsome.paths import cache_dir

CacheKind = Literal["analysis", "optical-tracks", "exposure"]


class CacheUsage(BaseModel):
    kind: str
    path: str
    bytes: int
    files: int
    reclaimable: bool
    limit_bytes: int | None
    note: str


class PreviewCacheUsage(BaseModel):
    active: bool
    bytes: int
    linear_bytes: int
    jpeg_bytes: int
    limit_bytes: int
    entries: int


# Audio responses and preview plans retain paths. They need reader leases before reclamation.
_CACHES = {
    "analysis": ("*.json", True, 500 << 20, "Saved reports; running analysis work is preserved."),
    "optical-tracks": ("*.npz", True, 512 << 20, "Four optical-track archives, at most 128 MiB each."),
    "exposure": ("*.json", True, None, "Measured exposure gains; active mounts keep their own copies."),
    "audio": ("*.wav", False, None, "Protected while playback can hold HTTP range readers."),
    "thumbs": ("*.jpg", False, None, "Protected while thumbnail responses can hold readers."),
    "preview": ("**/*", False, None, "Session-owned plans; released when the preview closes."),
}


def _entries(folder: Path, pattern: str) -> list[tuple[Path, int]]:
    if folder.is_symlink():
        return []
    result = []
    for path in folder.glob(pattern):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            result.append((path, info.st_size))
    return result


def usage() -> list[CacheUsage]:
    return [
        CacheUsage(
            kind=kind,
            path=str(cache_dir() / kind),
            bytes=sum(size for _, size in entries),
            files=len(entries),
            reclaimable=reclaimable,
            limit_bytes=limit,
            note=note,
        )
        for kind, (pattern, reclaimable, limit, note) in _CACHES.items()
        for entries in [_entries(cache_dir() / kind, pattern)]
    ]


def clear(kind: CacheKind) -> CacheUsage:
    """Unlink completed entries through an anchored directory descriptor; never recurse or follow links.

    Open-file readers retain their descriptors on Linux. All reclaimable consumers finish reading
    inside their own call and retain parsed data, not a pathname needed by a later reader.
    In-flight atomic writes and analysis work subdirectories are deliberately excluded.
    """
    folder = cache_dir() / kind
    pattern = _CACHES[kind][0]
    try:
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return next(item for item in usage() if item.kind == kind)
    try:
        for path, _ in _entries(folder, pattern):
            try:
                info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    os.unlink(path.name, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory)
    return next(item for item in usage() if item.kind == kind)
