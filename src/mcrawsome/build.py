"""This installation's build, so a window can tell an engine from another build with the same version."""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path


def package_hash(package: Path) -> str:
    """The first 12 hex digits of SHA-256 over the package's files (the Python sources, data and the built
    interface in server/web), by relative path and contents; compiled caches are left out."""
    digest = hashlib.sha256()
    for path in sorted(p for p in package.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@cache
def build_id() -> str:
    return package_hash(Path(__file__).parent)
