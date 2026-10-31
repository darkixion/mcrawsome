"""Fast Copy: recordings offloaded from a card or phone folder into a library folder.

A recording is a `.mcraw` (or the split parts `NAME.0.mcraw`, `NAME.1.mcraw` ...) with every file named
`NAME.*` beside it (`.gcsv` gyro data, `.json` and `.mcrawsome.toml` settings). Each file is copied to
`NAME.ext.part` while its SHA-256 is computed from the source, synced, read back from the disk and
compared, then renamed into place with the source's modification time. Nothing at the destination is
replaced: a file already there with the same contents is skipped, a different one is reported. An
interrupted copy leaves only its `.part` file, and the next copy keeps the blocks of it that match the
source and continues from the first that does not.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mcrawsome.errors import McError

CHUNK = 8 << 20  # bytes read and written at a time, and the block a resumed copy is checked in
_SPLIT = re.compile(r"^(?P<name>.+?)(?:\.(?P<part>\d+))?\.mcraw$", re.IGNORECASE)


class Recording(BaseModel):
    name: str
    files: list[Path]
    size: int


class CopyProgress(BaseModel):
    phase: Literal["checking", "copying", "verifying"]
    file: str
    done_bytes: int
    total_bytes: int
    files_done: int
    files_total: int
    bytes_per_s: float | None = None
    eta_s: float | None = None


class FileResult(BaseModel):
    source: Path
    target: Path
    status: Literal["copied", "skipped", "failed", "cancelled"]
    message: str
    sha256: str | None = None


def _base(path: Path) -> str | None:
    match = _SPLIT.match(path.name)
    return match["name"] if match else None


def _recording(folder: Path, name: str) -> Recording:
    files = sorted(
        (p for p in folder.iterdir() if p.name.startswith(name + ".") and p.is_file()),
        key=lambda p: p.name,
    )
    files = [p for p in files if _base(p) in (name, None) and not p.name.endswith(".part")]
    return Recording(name=name, files=files, size=sum(p.stat().st_size for p in files))


def find_recordings(sources: Iterable[Path]) -> list[Recording]:
    """The recordings in these folders (and their subfolders, not hidden ones) or beside these files."""
    found: dict[tuple[Path, str], Recording] = {}
    for source in sources:
        source = source.expanduser()
        if not source.exists():
            raise McError(f"{source} was not found", code="input-missing")
        if source.is_file():
            name = _base(source)
            if name is None:
                raise McError(f"{source.name} is not a MotionCam recording (.mcraw)", code="input-unreadable")
            found.setdefault((source.parent, name), _recording(source.parent, name))
            continue
        for folder, dirs, files in os.walk(source):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            here = Path(folder)
            for file in sorted(files):
                name = _base(Path(file))
                if name is not None and (here, name) not in found:
                    found[(here, name)] = _recording(here, name)
    return list(found.values())


def _hash_file(path: Path) -> str:
    """The SHA-256 of a file as it is on the disk, not as this process last wrote it."""
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # read back from the device where possible
        while block := os.read(fd, CHUNK):
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _same(source: Path, target: Path) -> bool:
    return source.stat().st_size == target.stat().st_size and _hash_file(source) == _hash_file(target)


class _Meter:
    """Bytes copied so far, the speed and the time left, for the progress callback."""

    def __init__(self, total: int, files: int, progress: Callable[[CopyProgress], None] | None) -> None:
        self.total, self.files, self.progress = total, files, progress
        self.done = self.files_done = self.moved = 0
        self.started = time.monotonic()

    def report(self, phase: Literal["checking", "copying", "verifying"], file: str) -> None:
        if self.progress is None:
            return
        elapsed = time.monotonic() - self.started
        speed = self.moved / elapsed if self.moved and elapsed > 0 else None
        eta = (self.total - self.done) / speed if speed else None
        self.progress(
            CopyProgress(
                phase=phase, file=file, done_bytes=self.done, total_bytes=self.total,
                files_done=self.files_done, files_total=self.files, bytes_per_s=speed, eta_s=eta,
            )
        )  # fmt: skip


class _CancelledError(Exception):
    pass


def _resume_point(source: int, part: Path) -> tuple[int, hashlib._Hash]:
    """How much of a `.part` file matches the source, in whole blocks, and the hash of that much."""
    digest = hashlib.sha256()
    kept = 0
    with part.open("rb") as copy:
        while True:
            theirs = copy.read(CHUNK)
            if not theirs:
                break
            ours = os.pread(source, len(theirs), kept)
            if len(theirs) < CHUNK or ours != theirs:  # a short last block is copied again too
                break
            digest.update(ours)
            kept += len(ours)
    return kept, digest


def _copy(source: Path, target: Path, meter: _Meter, stop: Callable[[], bool] | None, verify: bool) -> str:
    part = target.with_name(target.name + ".part")
    src = os.open(source, os.O_RDONLY)
    try:
        if part.exists():
            meter.report("checking", source.name)
            kept, digest = _resume_point(src, part)
        else:
            kept, digest = 0, hashlib.sha256()
        meter.done += kept
        out = os.open(part, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            os.ftruncate(out, kept)
            os.lseek(out, kept, os.SEEK_SET)
            position = kept
            while block := os.pread(src, CHUNK, position):
                if stop is not None and stop():
                    raise _CancelledError
                digest.update(block)
                view = memoryview(block)
                while view:
                    view = view[os.write(out, view) :]
                position += len(block)
                meter.done += len(block)
                meter.moved += len(block)
                meter.report("copying", source.name)
            os.fsync(out)
        finally:
            os.close(out)
    finally:
        os.close(src)
    sha = digest.hexdigest()
    if verify:
        meter.report("verifying", source.name)
        if _hash_file(part) != sha:
            part.unlink(missing_ok=True)
            raise McError(f"{target.name}: the copy did not verify (read back differently); try again",
                          code="verify-failed")  # fmt: skip
    stat = source.stat()
    os.utime(part, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    _publish(part, target)
    return sha


def _publish(part: Path, target: Path) -> None:
    """Rename the finished copy into place, never over a file that appeared meanwhile."""
    try:
        os.link(part, target)  # fails if the target exists
        part.unlink()
    except FileExistsError:
        part.unlink(missing_ok=True)
        raise McError(f"{target.name} appeared at the destination while copying; not replaced",
                      code="output-exists") from None  # fmt: skip
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK):
            raise
        if target.exists():  # a card or FAT folder without hard links: check, then rename
            raise McError(f"{target.name} appeared at the destination while copying; not replaced",
                          code="output-exists") from None  # fmt: skip
        os.rename(part, target)
    folder = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def offload_recordings(
    recordings: Iterable[Recording],
    destination: Path,
    *,
    verify: bool = True,
    progress: Callable[[CopyProgress], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> list[FileResult]:
    """Copy every file of these recordings into `destination` (created if needed), in order."""
    recordings = list(recordings)
    files = [path for recording in recordings for path in recording.files]
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise McError(f"Cannot create {destination}: {exc.strerror}", code="output-unwritable") from None
    meter = _Meter(sum(r.size for r in recordings), len(files), progress)
    results: list[FileResult] = []
    claimed: dict[str, Path] = {}
    cancelled = False
    for source in files:
        target = destination / source.name
        size, before = source.stat().st_size, meter.done
        if cancelled:
            results.append(FileResult(source=source, target=target, status="cancelled", message="Not copied"))
            continue
        try:
            if source.resolve() == target.resolve():
                raise McError(f"{source.name} is already in {destination}", code="output-exists")
            if target.name in claimed:
                raise McError(
                    f"{target.name}: a different file of that name was copied from "
                    f"{claimed[target.name].parent}",
                    code="output-exists",
                )
            claimed[target.name] = source
            if target.exists():
                meter.report("checking", source.name)
                if not _same(source, target):
                    raise McError(
                        f"{target.name}: a different file of that name is already at the destination; "
                        "not replaced", code="output-exists",
                    )  # fmt: skip
                meter.done = before + size
                result = FileResult(source=source, target=target, status="skipped", message="Already copied")
            else:
                sha = _copy(source, target, meter, stop, verify)
                message = "Copied and verified" if verify else "Copied"
                result = FileResult(
                    source=source, target=target, status="copied", message=message, sha256=sha
                )
        except _CancelledError:
            cancelled = True
            result = FileResult(
                source=source, target=target, status="cancelled",
                message=f"Stopped; {target.name}.part is kept and the next copy continues it",
            )  # fmt: skip
        except McError as exc:
            meter.done = before + size
            result = FileResult(source=source, target=target, status="failed", message=exc.message)
        except OSError as exc:
            meter.done = before + size
            message = f"{source.name}: {exc.strerror or exc}"
            result = FileResult(source=source, target=target, status="failed", message=message)
        meter.files_done += result.status != "cancelled"
        results.append(result)
        meter.report("copying", source.name)
    return results
