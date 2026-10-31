"""Synced atomic replacement for small sidecars and settings documents."""

import fcntl
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def write_atomic(path: Path, content: bytes) -> None:
    """Replace `path` with `content` through a synced temporary file, keeping the file's mode (else the
    umask's default)."""
    try:
        mode: int | None = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = None
    part = path.with_name(f"{path.name}.{secrets.token_hex(4)}.part")
    try:
        with part.open("xb") as file:  # created as 0666 less the umask
            if mode is not None:
                os.fchmod(file.fileno(), mode)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


@contextmanager
def parent_lock(path: Path) -> Iterator[None]:
    """Coordinate settings replacement with other writers locking the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
