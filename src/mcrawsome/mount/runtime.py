"""Pin Debian-owned mount code and vendor dependencies until its last filesystem exits."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mcrawsome.appimage import launcher
from mcrawsome.errors import McError
from mcrawsome.io.atomic import write_atomic
from mcrawsome.paths import state_dir

LAUNCHER = "mcrawsome-mount-service"
BOOTSTRAP = """import sys
from importlib.machinery import BuiltinImporter, FrozenImporter, PathFinder
from pathlib import Path
sys.dont_write_bytecode = True
sys.meta_path[:] = [BuiltinImporter, FrozenImporter, PathFinder]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcrawsome.mount.service import main
main()
"""


def _package() -> Path:
    return Path(__file__).resolve().parent.parent


def _manifest(package: Path) -> dict[str, object] | None:
    marker = package / "MOUNT_RUNTIME"
    if not marker.is_file():
        return None  # A developer environment or the AppImage, which starts services through itself.
    try:
        data = json.loads(marker.read_bytes())
        if data["protocol"] != 1 or not re.fullmatch(r"[0-9a-f]{64}", data["id"]):
            raise ValueError("Unsupported mount runtime")
        if not Path(data["vendor"]).is_absolute():
            raise ValueError("Mount vendor folder must be absolute")
        return dict(data)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise McError(f"Could not read mount runtime: {exc}", code="input-unreadable") from None


def _start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def _used(root: Path) -> set[str] | None:
    """Find pinned launchers, including a child that has not published ready.json yet."""
    used = set()
    for lease in root.iterdir():
        if not re.fullmatch(r"\.lease-[0-9a-f]{32}", lease.name):
            continue
        try:
            data = json.loads(lease.read_bytes())
            if data["start"] is not None and _start(data["pid"]) == data["start"]:
                used.add(data["runtime"])
            else:
                lease.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            return None
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        except OSError:
            return None  # Unknown live processes: retain runtimes rather than guessing.
        for argument in arguments:
            path = Path(os.fsdecode(argument))
            if path.name == LAUNCHER and path.parent.parent == root:
                used.add(path.parent.name)
    return used


def _collect(root: Path, keep: str | None = None) -> None:
    used = _used(root)
    if used is None:
        return
    if keep:
        used.add(keep)
    for folder in root.iterdir():
        if re.fullmatch(r"[0-9a-f]{64}", folder.name) and folder.name not in used and not folder.is_symlink():
            shutil.rmtree(folder)


@contextmanager
def _locked() -> Iterator[Path]:
    root = state_dir() / "mount-runtimes"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise OSError("Unexpected mount runtime directory owner")
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield root


def collect_runtimes() -> None:
    """Only application-owned copies, never source recordings or live service code."""
    if not (state_dir() / "mount-runtimes").is_dir():
        return
    try:
        with _locked() as root:
            _collect(root)
    except OSError:
        pass  # Cleanup must not prevent normal unmounting.


@contextmanager
def runtime_command() -> Iterator[list[str]]:
    """Reserve a runtime through Popen without serialising independent service starts."""
    package = _package()
    manifest = _manifest(package)
    if manifest is None:
        # From the AppImage, through the image: the service then has its own copy of the program for as
        # long as it serves, whatever happens to the engine's copy.
        image = launcher()
        yield [image, "mount-service"] if image else [sys.executable, "-m", "mcrawsome.mount.service"]
        return
    identity = str(manifest["id"])
    lease: Path | None = None
    try:
        with _locked() as root:
            destination = root / identity
            if destination.is_symlink():
                raise OSError("Refusing a symbolic link for mount runtime")
            _collect(root, identity)
            if not destination.exists():
                with tempfile.TemporaryDirectory(prefix=".copy-", dir=root) as scratch:
                    copied = Path(scratch) / identity
                    shutil.copytree(
                        package, copied / "mcrawsome", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                    )
                    vendor = Path(str(manifest["vendor"]))
                    shutil.copytree(
                        vendor, copied / "vendor", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                    )
                    (copied / "mcrawsome/VENDOR").write_text(str(destination / "vendor") + "\n")
                    (copied / LAUNCHER).write_text(BOOTSTRAP)
                    (copied / "runtime.json").write_text(json.dumps({"id": identity, "protocol": 1}))
                    copied.rename(destination)
            if json.loads((destination / "runtime.json").read_bytes()) != {"id": identity, "protocol": 1}:
                raise OSError("Incomplete mount runtime")
            lease = root / f".lease-{uuid.uuid4().hex}"
            write_atomic(
                lease,
                json.dumps({"runtime": identity, "pid": os.getpid(), "start": _start(os.getpid())}).encode(),
            )
        yield [sys.executable, "-I", str(destination / LAUNCHER)]
    except (OSError, ValueError) as exc:
        raise McError(f"Could not preserve mount runtime: {exc}", code="output-unwritable") from None
    finally:
        if lease is not None:
            lease.unlink(missing_ok=True)


def running_runtime() -> dict[str, object] | None:
    """Handshake recorded by a service that actually loaded the pinned package."""
    root = _package().parent
    try:
        data = json.loads((root / "runtime.json").read_bytes())
        if root.name == data["id"] and data["protocol"] == 1:
            return {**data, "path": str(root)}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None
