"""Resumable exports: a work folder beside the output holding a manifest and the frames finished so far.

The manifest records what was asked for and what was measured, so running the same command again after an
interruption keeps the finished frames, and a change of settings starts again. The folder is removed once
the output is published.
"""

from __future__ import annotations

import errno
import fcntl
import functools
import hashlib
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Concatenate

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcrawsome import __version__
from mcrawsome.errors import ErrorCode, McError, error_code
from mcrawsome.output.sidecar import quick_hash, sidecar_path
from mcrawsome.recipe.schema import Recipe

CHUNK_FRAMES = 240  # recorded in each manifest, so work folders from earlier versions still match
SETTINGS_CHANGED = "settings changed since the interrupted export; starting again"
NOTHING_KEPT = "the interrupted export stopped before anything was kept; starting again"

_STRICT = ConfigDict(extra="forbid")


class InputFile(BaseModel):
    model_config = _STRICT

    path: str
    size: int
    sha256_first_16mib: str

    @classmethod
    def of(cls, path: Path) -> InputFile:
        return cls(path=str(path.resolve()), size=path.stat().st_size, sha256_first_16mib=quick_hash(path))


class GyroFile(BaseModel):
    model_config = _STRICT

    path: str
    size: int
    sha256: str


def gyro_input(clip: Path, recipe: Recipe) -> GyroFile | None:
    """The external RAW gyro log an MCRAW copy carries with it (exports are not stabilised)."""
    if clip.suffix.lower() != ".mcraw" or recipe.output.kind != "mcraw":
        return None
    path = clip.with_suffix(".gcsv")
    if not path.exists():
        return None
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return GyroFile(path=str(path.resolve()), size=path.stat().st_size, sha256=digest)


class Encoder(BaseModel):
    """The encoder and its arguments (without the program and the output file), the same for every chunk."""

    model_config = _STRICT

    name: str
    codec: str
    args: list[str]


class Manifest(BaseModel):
    model_config = _STRICT

    mcrawsome_version: str
    input: InputFile
    gyro: GyroFile | None = None
    requested: Recipe  # as given
    resolved: Recipe  # after resolve_auto
    measurements: dict[str, Any]
    notes: list[str] = Field(default_factory=list)  # what the measuring and planning had to say
    frame_range: tuple[int, int] | None
    chunk_frames: int
    encoder: Encoder
    picture: str = ""  # fingerprint of the baked look and the per-frame settings (see export.picture_key)
    light: tuple[int, int] | None = None  # measured MaxCLL, MaxFALL, passed to every chunk's encode
    plan_file: str | None = None  # the stabilisation plan, in the work folder
    frame_gains: list[float] | None = None  # deflicker: the measured gain of each recorded frame


_MATCHING = (
    "mcrawsome_version",
    "input",
    "gyro",
    "requested",
    "frame_range",
    "chunk_frames",
    "encoder",
    "picture",
)


def request(
    clip: Path, requested: Recipe, frame_range: tuple[int, int] | None, encoder: Encoder, picture: str = ""
) -> dict[str, Any]:
    """The manifest fields that say what is asked for: an interrupted export resumes only when all match.
    The Recipe names a look but a user look file can change, so `picture` fingerprints what it gives."""
    return {
        "mcrawsome_version": __version__,
        "input": InputFile.of(clip),
        "gyro": gyro_input(clip, requested),
        "requested": requested,
        "frame_range": frame_range,
        "chunk_frames": CHUNK_FRAMES,
        "encoder": encoder,
        "picture": picture,
    }


class WorkFolder:
    """`.<output name>.mcrawsome-work` beside the output. Its file names are fixed, so no output name can
    clash with them."""

    def __init__(self, output: Path) -> None:
        output = output.absolute()
        self.path = output.parent / f".{output.name}.mcrawsome-work"
        self.publish_path = self.path / f"publish{output.suffix}"
        self._manifest = self.path / "manifest.json"

    def load(self) -> Manifest | None:
        try:
            return Manifest.model_validate_json(self._manifest.read_bytes())
        except (OSError, ValidationError):
            return None

    def save(self, manifest: Manifest) -> None:
        part = self._manifest.with_suffix(".part")
        part.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        part.replace(self._manifest)

    def publish_interrupted(self, output: Path) -> bool:
        """True when the output is in place but the run stopped before the prepared sidecar was moved into
        place and this folder discarded (the staged output or the staged DNG frames are gone)."""
        return output.exists() and not self.publish_path.exists() and sidecar_path(self.publish_path).exists()

    def matches(self, manifest: Manifest) -> bool:
        stored = self.load()
        return stored is not None and all(getattr(stored, k) == getattr(manifest, k) for k in _MATCHING)

    def discard(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)


def lock_path(output: Path) -> Path:
    """Beside the work folder, not in it: the folder is discarded and made again while the lock is held."""
    output = output.absolute()
    return output.parent / f".{output.name}.mcrawsome-lock"


@contextmanager
def export_lock(output: Path) -> Iterator[None]:
    """An exclusive lock on the output's work folder, for one export or a discard of its work. Raises
    "output-exists" at once when another process holds it. The lock file is removed on release."""
    path = lock_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(fd, 32).decode(errors="replace").strip() or "unknown"
            os.close(fd)
            raise McError(
                f"another MCRAWsome process (pid {holder}) is exporting to {output}", code="output-exists"
            ) from None
        try:
            current = os.stat(path)
        except FileNotFoundError:
            current = None
        opened = os.fstat(fd)
        if current is not None and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
            break
        os.close(fd)  # the holder removed this file as we opened it: lock the one there now
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        path.unlink(missing_ok=True)  # while still held, so a process waiting on this file tries again
        os.close(fd)


def locked_output[**P, R](
    export: Callable[Concatenate[Path, Path, P], R],
) -> Callable[Concatenate[Path, Path, P], R]:
    """Hold the output's lock for the whole of an export(clip, output, ...)."""

    @functools.wraps(export)
    def locked(clip: Path, output: Path, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with export_lock(output):
            try:
                return export(clip, output, *args, **kwargs)
            except OSError as exc:  # not caught on the way: a full disk, or the output side, by its file
                raise McError(f"{clip.name}: {exc}", code=_os_error_code(exc, output)) from None

    return locked


def _os_error_code(exc: OSError, output: Path) -> ErrorCode:
    """disk-full, output-unwritable for a file the export writes (the output, its sidecar or gyro log, the
    work folder, the lock or the folder they are in), and otherwise what error_code says."""
    if exc.errno == errno.ENOSPC:
        return "disk-full"
    output = output.absolute()
    ours = {output, output.parent, sidecar_path(output), output.with_suffix(".gcsv"), lock_path(output)}
    work = WorkFolder(output).path
    for name in (exc.filename, exc.filename2):
        if isinstance(name, str | os.PathLike):
            path = Path(os.fsdecode(name)).absolute()
            if path in ours or path.is_relative_to(output) or path.is_relative_to(work):
                return "output-unwritable"
    return error_code(exc, default="input-unreadable")


def resume_or_restart(
    work: WorkFolder, restart: bool, candidate: Callable[[Manifest], Manifest | None]
) -> tuple[Manifest | None, list[str]]:
    """The stored manifest when the interrupted export asked for the same thing (`candidate(stored)` is this
    request's manifest, or None when it cannot be the same). Otherwise None and an empty work folder, with
    a note saying why an interrupted export starts again."""
    interrupted = work.path.exists() and not restart
    stored = work.load() if interrupted else None
    if stored is not None:
        wanted = candidate(stored)
        if wanted is not None and work.matches(wanted):
            return stored, []
    work.discard()
    work.path.mkdir(parents=True)
    if not interrupted:
        return None, []
    return None, [SETTINGS_CHANGED if stored is not None else NOTHING_KEPT]
