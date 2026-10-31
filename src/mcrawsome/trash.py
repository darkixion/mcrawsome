"""Moving files to the desktop's Trash, as the freedesktop.org Trash specification (1.0) describes, so
the file manager can restore them. Nothing is deleted."""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

COMPANION_SUFFIXES = (".gcsv", ".meta", ".csv", ".mcrawsome.toml")


def companions(recording: Path) -> list[Path]:
    """The recording and the files written beside it for it: the gyro log, MotionCam's capture metadata
    and CSV, and its own settings layer."""
    names = [recording.stem + suffix for suffix in COMPANION_SUFFIXES] + [recording.name + ".csv"]
    found = [recording.with_name(name) for name in dict.fromkeys(names)]
    return [recording, *(path for path in found if path.is_file() and not path.is_symlink())]


def split_parts(recordings: list[Path]) -> list[Path]:
    """Other parts of split recordings (name.0.mcraw, name.1.mcraw...) beside these, not among them."""
    chosen = {path.absolute() for path in recordings}
    found: set[Path] = set()
    for path in chosen:
        match = re.fullmatch(r"(.+)\.\d+(\.mcraw)", path.name, re.IGNORECASE)
        if match:
            pattern = re.compile(re.escape(match[1]) + r"\.\d+" + re.escape(match[2]), re.IGNORECASE)
            found.update(sibling for sibling in path.parent.iterdir() if pattern.fullmatch(sibling.name))
    return sorted(found - chosen)


def home_trash() -> Path:
    data = os.environ.get("XDG_DATA_HOME")
    return (Path(data) if data and os.path.isabs(data) else Path.home() / ".local/share") / "Trash"


def _top(path: Path) -> Path:
    """The mount point holding `path`."""
    device = path.lstat().st_dev
    while path.parent != path and path.parent.stat().st_dev == device:
        path = path.parent
    return path


def _usable(folder: Path, *, shared: bool) -> bool:
    try:
        info = folder.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if shared:  # $topdir/.Trash is shared by every user: it must be sticky
        return bool(info.st_mode & stat.S_ISVTX)
    return info.st_uid == os.getuid()


def trash_for(path: Path) -> tuple[Path, Path | None]:
    """The Trash folder for `path`, and the top folder Path= entries are relative to (None: absolute)."""
    home = home_trash()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if home.lstat().st_dev == path.lstat().st_dev:
        return home, None
    top = _top(path)
    uid = os.getuid()
    shared = top / ".Trash"
    if _usable(shared, shared=True) and not shared.is_symlink():
        own = shared / str(uid)
        own.mkdir(exist_ok=True, mode=0o700)
        if _usable(own, shared=False):
            return own, top
    own = top / f".Trash-{uid}"
    own.mkdir(exist_ok=True, mode=0o700)
    if not _usable(own, shared=False):
        raise PermissionError(f"{own} is not a usable Trash folder")
    return own, top


def move_to_trash(path: Path, *, now: datetime | None = None) -> Path:
    """Move `path` into its Trash and return where it went. The information file is written first, so an
    interrupted move leaves no file in the Trash that cannot be restored."""
    path = path.absolute()
    trash, top = trash_for(path)
    files, info = trash / "files", trash / "info"
    files.mkdir(exist_ok=True, mode=0o700)
    info.mkdir(exist_ok=True, mode=0o700)
    recorded = path.relative_to(top) if top is not None else path
    when = (now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    body = f"[Trash Info]\nPath={quote(str(recorded))}\nDeletionDate={when}\n".encode()
    stem, suffix = path.stem, path.suffix
    for attempt in range(1, 10_000):
        name = path.name if attempt == 1 else f"{stem}.{attempt}{suffix}"
        entry = info / f"{name}.trashinfo"
        try:
            descriptor = os.open(entry, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        target = files / name
        if target.exists() or target.is_symlink():
            entry.unlink()
            continue
        try:
            os.rename(path, target)
        except BaseException:
            entry.unlink(missing_ok=True)
            raise
        return target
    raise FileExistsError(f"No free name for {path.name} in {trash}")
