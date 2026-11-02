"""Persistent mount identities and owned filesystem services, independent of editor imports."""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from types import EllipsisType

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcrawsome.appimage import host_environment
from mcrawsome.camera.vignette import VignetteProfile
from mcrawsome.errors import McError
from mcrawsome.io.timecode import TIMECODE_VERSION, local_zone
from mcrawsome.mount.limits import MAX_MOUNTS, work_slot
from mcrawsome.mount.runtime import LAUNCHER
from mcrawsome.mount.sequence import Sequence, source_stamp
from mcrawsome.paths import mount_dir, state_dir
from mcrawsome.processes import spawn, stop_unit
from mcrawsome.recipe.raw import RawCalibration

logger = logging.getLogger(__name__)


class MountedSequence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: Path
    path: Path
    mount_root: Path | None = None
    stamp: tuple[int, int, int, int]
    vignette_profile: VignetteProfile | None = None
    colour_matrix: tuple[float, ...] | None = (
        None  # camera.colour_matrix: XYZ to camera, replacing the clip's
    )
    raw_settings: RawCalibration = Field(default_factory=RawCalibration)
    frame_range: tuple[int, int] | None = None  # (first output frame, count): the clip's in and out marks
    quad_bayer_remosaic: bool = True
    timecode: str | None = None  # the time zone of time-of-day timecode; None numbers frames from zero
    frames: int = 0
    active: bool = False
    serving: bool = False
    source_available: bool = True
    sent: bool = False
    retained: bool = False
    in_use: bool | None = None


class MountListing(BaseModel):
    available: bool
    reason: str
    mounts: list[MountedSequence]


def availability() -> tuple[bool, str]:
    if sys.platform != "linux":
        return False, "Virtual CinemaDNG needs Linux FUSE; use Export for CinemaDNG files"
    try:
        importlib.import_module("mcrawsome._fuse")
    except ImportError:
        return False, "This build has no FUSE adapter; use mcrawsome export --to dng on the command line"
    if not os.access("/dev/fuse", os.R_OK | os.W_OK) or helper() is None:
        return (
            False,
            "FUSE needs /dev/fuse and fusermount3; use mcrawsome export --to dng on the command line",
        )
    return True, "Virtual CinemaDNG is available"


def helper() -> str | None:
    # libfuse3 mounts through fusermount3; FUSE 2's fusermount cannot stand in for it.
    return shutil.which("fusermount3")


FUSE_TYPE = " - fuse.mcrawsome "


def is_mounted(path: Path) -> bool:
    # Reading mountinfo also works for a disconnected FUSE server, without blocking on its stat().
    escaped = (
        str(path).replace("\\", r"\134").replace(" ", r"\040").replace("\t", r"\011").replace("\n", r"\012")
    )
    try:
        return any(
            line.split()[4] == escaped and FUSE_TYPE in line
            for line in Path("/proc/self/mountinfo").read_text().splitlines()
        )
    except OSError:
        return False


def mounted_sources() -> set[Path]:
    """The recordings with media mounted, by their records, as a mount outlives the engine that made it."""
    sources = set()
    for record in (state_dir() / "mounts").glob("*/sequence.json"):
        try:
            mount = json.loads(record.read_bytes())
            if is_mounted(Path(mount["path"])):
                sources.add(Path(mount["source"]).resolve())
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sources


def process_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def service_running(folder: Path) -> bool:
    try:
        ready = json.loads((folder / "ready.json").read_text())
        pid = int(ready["pid"])
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return (
            ready["start"] == process_start(pid)
            # python -m mcrawsome.mount.service, or mcrawsome.cli.main mount-service through an AppImage, or
            # a pinned runtime's launcher
            and (
                b"mcrawsome.mount.service" in command
                or b"mount-service" in command
                or any(Path(os.fsdecode(item)).name == LAUNCHER for item in command)
            )
            and os.fsencode(folder) in command
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def usage(folder: Path) -> bool | None:
    try:
        ready = json.loads((folder / "ready.json").read_text())
        value = json.loads((folder / "usage.json").read_text())
        pid = int(ready["pid"])
        stamp = process_start(pid)
        if (
            stamp is not None
            and ready["start"] == value["start"] == stamp
            and value["pid"] == pid
            and type(value["in_use"]) is bool
        ):
            return bool(value["in_use"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Signal the session a service was started in (it leads its own), reaching a service started
    behind an AppImage launcher that does not pass signals on."""
    if stop_unit(process, sig):
        return
    with suppress(ProcessLookupError, PermissionError):
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, sig)


HISTORY_LIMIT = 1 << 20  # bytes in mount-history.log before it is moved to mount-history.log.1
# How long an unmount refused as busy is tried again: GNOME's trash service looks for .Trash folders in every
# new mount, and a refusal while that lookup is under way would otherwise report media as in use.
UNMOUNT_WAIT_S = 1.0


def log_removed(mount: MountedSequence | None, identity: str) -> None:
    """Append a removed record to state_dir()/mount-history.log as one JSON line."""
    history = state_dir() / "mount-history.log"
    entry: dict[str, object] = {"removed": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": identity}
    if mount is not None:
        entry |= mount.model_dump(mode="json", exclude={"active", "serving", "in_use"})
    with suppress(OSError):
        if history.stat().st_size >= HISTORY_LIMIT:
            history.replace(history.with_name(history.name + ".1"))
    with suppress(OSError), history.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def save_record(folder: Path, mount: MountedSequence) -> None:
    """Default RAW settings and a whole-recording range are left out, so an older engine that does not know
    a newer setting can still read a record that does not use it."""
    record = mount.model_dump(mode="json", exclude={"in_use"})
    record["raw_settings"] = mount.raw_settings.model_dump(mode="json", exclude_defaults=True)
    if mount.frame_range is None:
        del record["frame_range"]
    if mount.quad_bayer_remosaic:
        del record["quad_bayer_remosaic"]  # old records and engines default to enabled
    if mount.colour_matrix is None:
        del record["colour_matrix"]
    temporary = folder / "sequence.part"
    temporary.write_text(json.dumps(record))
    temporary.replace(folder / "sequence.json")


def starting(folder: Path) -> bool:
    try:
        value = json.loads((folder / "starting.json").read_text())
        stamp = process_start(int(value["pid"]))
        return stamp is not None and stamp == value["start"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def timecode_zone(choice: str, zone: str, source: Path) -> str | None:
    """The time zone of a new mount's time-of-day timecode, or None for timecode from zero: this
    computer's, or (when chosen and recorded) the recording location's, from its GPS fix."""
    if choice != "time-of-day":
        return None
    if zone == "location":
        from mcrawsome.io.clip import Clip
        from mcrawsome.library.places import recording_zone

        with Clip(source, metadata_only=True) as clip:
            found = recording_zone(clip.metadata.gps)
        if found is not None:
            return found
    return local_zone()


def prepare(
    source: Path,
    vignette_profile: VignetteProfile | None,
    raw_settings: RawCalibration,
    frame_range: tuple[int, int] | None,
    quad_bayer_remosaic: bool = True,
    colour_matrix: tuple[float, ...] | None = None,
    timecode: str | None = None,
) -> None:
    """Open the sequence once before its service starts: settings it cannot serve are refused here with
    their reason, rather than as a failed service."""
    try:
        with Sequence(
            source,
            vignette_profile=vignette_profile,
            raw_settings=raw_settings,
            frame_range=frame_range,
            quad_bayer_remosaic=quad_bayer_remosaic,
            colour_matrix=colour_matrix,
            timecode=timecode,
        ):
            pass
    except ValueError as exc:
        raise McError(str(exc), code="recipe-invalid") from None


class MountManager:
    def __init__(self) -> None:
        self.root = (state_dir() / "mounts").absolute()
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.restoring: set[str] = set()  # sent media being mounted again at start-up: never pruned

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock, (self.root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextmanager
    def _identity_locked(self, identity: str) -> Iterator[None]:
        if not re.fullmatch(r"[0-9a-f]{24}", identity):
            raise McError("No such mounted sequence", code="input-missing")
        folder = self.root / identity
        while True:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (folder / ".operation.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    current = (folder / ".operation.lock").stat().st_ino == os.fstat(lock.fileno()).st_ino
                except FileNotFoundError:
                    current = False
                if current:  # else the folder was pruned while this waited: lock the new one
                    yield
                    return

    def _read(self, identity: str) -> MountedSequence:
        if not re.fullmatch(r"[0-9a-f]{24}", identity):
            raise McError("No such mounted sequence", code="input-missing")
        try:
            mount = MountedSequence.model_validate_json((self.root / identity / "sequence.json").read_bytes())
        except (OSError, ValidationError):
            raise McError("No such mounted sequence", code="input-missing") from None
        expected_path = (
            mount.mount_root / f"{mount.source.stem}-{identity[:8]}"
            if mount.mount_root is not None
            else self.root / identity / "media"
        )
        if mount.id != identity or mount.path != expected_path:
            raise McError("Invalid mount record", code="input-unreadable")
        mount.active = is_mounted(mount.path)
        mount.serving = mount.active and service_running(self.root / identity)
        mount.in_use = usage(self.root / identity) if mount.serving else None
        try:
            mount.source_available = source_stamp(mount.source) == mount.stamp
        except OSError:
            mount.source_available = False
        self._clean_mountpoint(mount)
        return mount

    def _clean_mountpoint(self, mount: MountedSequence) -> None:
        """Remove only an empty, inactive mountpoint; records remain available for restoration.

        Call under the shared manager lock so a concurrent start cannot lose its directory.
        """
        folder = self.root / mount.id
        process = self.processes.get(mount.id)
        if (
            mount.active
            or starting(folder)
            or service_running(folder)
            or (process is not None and process.poll() is None)
            or mount.path.is_symlink()
        ):
            return
        with suppress(OSError):
            mount.path.rmdir()  # Atomic and non-recursive: non-empty directories are preserved.

    def prune(self, identities: Iterable[str]) -> set[str]:
        """Log and delete the records of these mounts that are no longer mounted, starting or served.
        Mounting or sending the same recording with the same settings again gives the same identity
        and media path. Returns the identities removed."""
        removed: set[str] = set()
        for identity in identities:
            if identity in self.restoring:
                continue
            with self._identity_locked(identity), self._locked():
                folder = self.root / identity
                process = self.processes.get(identity)
                if (
                    starting(folder)
                    or service_running(folder)
                    or (process is not None and process.poll() is None)
                ):
                    continue
                try:
                    mount: MountedSequence | None = self._read(identity)
                except McError:
                    mount = None
                if mount is not None and mount.active:
                    continue
                log_removed(mount, identity)
                self.processes.pop(identity, None)
                shutil.rmtree(folder, ignore_errors=True)
                removed.add(identity)
        return removed

    def get(self, identity: str) -> MountedSequence:
        with self._locked():
            return self._read(identity)

    def listing(self) -> MountListing:
        """The mounted sequences; records of media no longer mounted are logged and removed."""
        available, reason = availability()
        with self._locked():
            mounts = []
            stale = []
            for path in sorted(self.root.iterdir()):
                if re.fullmatch(r"[0-9a-f]{24}", path.name):
                    try:
                        mount = self._read(path.name)
                    except McError:
                        if not starting(path):
                            stale.append(path.name)
                        continue
                    if mount.active or starting(path):
                        mounts.append(mount)
                    else:
                        stale.append(path.name)
            for identity, process in list(self.processes.items()):
                if process.poll() is not None:
                    del self.processes[identity]
        kept = set(stale) - self.prune(stale)  # it started or is being served meanwhile: list it
        if kept:
            with self._locked():
                for identity in sorted(kept):
                    with suppress(McError):
                        mounts.append(self._read(identity))
            mounts.sort(key=lambda mount: mount.id)
        return MountListing(available=available, reason=reason, mounts=mounts)

    def usage_snapshot(self) -> dict[str, bool | None]:
        with self._locked():
            return {
                folder.name: usage(folder)
                for folder in self.root.iterdir()
                if re.fullmatch(r"[0-9a-f]{24}", folder.name)
            }

    def retain(self, identities: list[str]) -> None:
        """Persist dependencies before dispatch, after the archive has captured these generations."""
        with ExitStack() as locks:
            for identity in sorted(set(identities)):
                locks.enter_context(self._identity_locked(identity))
            locks.enter_context(self._locked())
            mounts = [self._read(identity) for identity in dict.fromkeys(identities)]
            if any(not m.active or not m.serving or not m.source_available for m in mounts):
                raise McError("Sent media is unavailable; mount it again", code="input-unreadable")
            for mount in mounts:
                mount.sent = mount.retained = True
                save_record(self.root / mount.id, mount)

    def sent_offline(self) -> list[str]:
        """Sent media no longer mounted (after a restart, say) whose recordings are unchanged: what an
        editor's projects still name. Claimed at once, so no listing prunes them before `restore`."""
        if not self.root.is_dir():
            return []
        found = []
        with self._locked():
            for folder in sorted(self.root.iterdir()):
                if not re.fullmatch(r"[0-9a-f]{24}", folder.name):
                    continue
                with suppress(McError):
                    mount = self._read(folder.name)
                    if mount.retained and not mount.active and mount.source_available:
                        found.append(mount.id)
            self.restoring.update(found)
        return found

    def restore(self, identities: list[str]) -> list[MountedSequence]:
        """Mount sent media again under the same paths, one at a time; a failure is logged and skipped."""
        restored = []
        for identity in identities:
            try:
                restored.append(self.remount(identity))
            except McError as exc:
                logger.warning("Could not mount %s again at start-up: %s", identity, exc.message)
            finally:
                self.restoring.discard(identity)
        return restored

    def remount(self, identity: str) -> MountedSequence:
        with self._identity_locked(identity):
            with self._locked():
                mount = self._read(identity)
            if not mount.source_available:
                raise McError(
                    "Source changed or is offline; send the recording again", code="input-unreadable"
                )
            if mount.active:
                self._unmount(mount.path, dead=not mount.serving)
            if mount.sent:
                mount.retained = True
            if (
                mount.raw_settings.mount_normalise_exposure == "picture"
            ):  # measured again if the gains are gone
                prepare(
                    mount.source,
                    mount.vignette_profile,
                    mount.raw_settings,
                    mount.frame_range,
                    mount.quad_bayer_remosaic,
                    mount.colour_matrix,
                )
            return self._start(mount)

    def mount(
        self,
        source: Path,
        *,
        expected: tuple[int, int, int, int] | None = None,
        location: Path | None = None,
        vignette_profile: VignetteProfile | None = None,
        raw_settings: RawCalibration | None = None,
        frame_range: tuple[int, int] | None = None,
        quad_bayer_remosaic: bool | None = None,
        colour_matrix: tuple[float, ...] | None = None,
        timecode: str | EllipsisType | None = ...,
    ) -> MountedSequence:
        """`timecode` is the time zone of time-of-day timecode, or None for timecode from zero; by default
        it follows the Settings choice when mounting."""
        available, reason = availability()
        if not available:
            raise McError(reason, code="input-unreadable")
        try:
            source = source.resolve(strict=True)
            stamp = source_stamp(source)
        except OSError as exc:
            raise McError(f"Source is offline: {exc}", code="input-unreadable") from None
        if expected is not None and expected != stamp:
            raise McError("Source changed or is offline; send the recording again", code="input-unreadable")
        from mcrawsome.server.settings import load_settings

        preferences, _ = load_settings()
        if quad_bayer_remosaic is None:
            quad_bayer_remosaic = preferences.quad_bayer_remosaic
        if isinstance(timecode, EllipsisType):
            timecode = timecode_zone(preferences.timecode, preferences.timecode_zone, source)
        selected = location or Path(
            os.environ.get("MCRAWSOME_MOUNT_DIR") or preferences.mount_folder or mount_dir()
        )
        selected = selected.expanduser().resolve()
        key: list[object] = [2, str(source), stamp, str(selected)]
        if vignette_profile is not None:
            key.append(vignette_profile.model_dump(mode="json"))
        if colour_matrix is not None:
            colour_matrix = tuple(float(value) for value in colour_matrix)
            key.append({"colour_matrix": list(colour_matrix)})
        raw_settings = raw_settings or RawCalibration()
        if values := raw_settings.model_dump(mode="json", exclude_defaults=True):
            key.append({"raw_settings": values})
        if raw_settings.mount_camera_model == "Panasonic Varicam RAW":
            key.append({"panasonic_dng_exposure_version": 1})
        if frame_range is not None:
            key.append({"frame_range": list(frame_range)})
        if not quad_bayer_remosaic:
            key.append({"quad_bayer_remosaic": False})
        if timecode is not None:  # served bytes differ: a new immutable generation
            key.append({"timecode": timecode, "timecode_version": TIMECODE_VERSION})
        identity = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
        prepare(
            source, vignette_profile, raw_settings, frame_range, quad_bayer_remosaic, colour_matrix, timecode
        )
        with self._identity_locked(identity):
            with self._locked():
                previous = self._read(identity) if (self.root / identity / "sequence.json").exists() else None
            # A process can still be alive while its virtual files are unreadable.
            # Recover this just like a disconnected service, using a normal unmount
            # so existing editor handles still prevent any replacement.
            if previous is not None and previous.active:
                from mcrawsome.mount.readiness import readable_media

                recover = not previous.serving
                if not recover:
                    try:
                        readable_media(previous)
                    except ValueError:
                        recover = True
                if recover:
                    self._unmount(previous.path, dead=not previous.serving)
            return self._start(
                MountedSequence(
                    id=identity,
                    source=source,
                    path=selected / f"{source.stem}-{identity[:8]}",
                    mount_root=selected,
                    stamp=stamp,
                    vignette_profile=vignette_profile,
                    colour_matrix=colour_matrix,
                    raw_settings=raw_settings,
                    frame_range=frame_range,
                    quad_bayer_remosaic=quad_bayer_remosaic,
                    timecode=timecode,
                    sent=previous.sent if previous else False,
                    retained=previous.retained if previous else False,
                )
            )

    def _start(self, mount: MountedSequence) -> MountedSequence:
        """Reserve capacity briefly, then start under this identity's lock without blocking others."""
        available, reason = availability()
        if not available:
            raise McError(reason, code="input-unreadable")
        try:
            if source_stamp(mount.source) != mount.stamp:
                raise McError(
                    "Source changed or is offline; send the recording again", code="input-unreadable"
                )
        except OSError:
            raise McError(
                "Source changed or is offline; send the recording again", code="input-unreadable"
            ) from None
        identity, path = mount.id, mount.path
        folder = self.root / identity
        with self._locked():
            if is_mounted(path):
                existing = self._read(identity)
                existing.retained = mount.retained
                if not existing.source_available or not existing.serving:
                    raise McError(
                        "Mount service stopped or source changed; remount the sequence",
                        code="input-unreadable",
                    )
                save_record(folder, existing)
                return existing
            active = 0
            for item in self.root.iterdir():
                if re.fullmatch(r"[0-9a-f]{24}", item.name):
                    try:
                        active += self._read(item.name).active or starting(item)
                    except McError:
                        continue
            if active >= MAX_MOUNTS:
                raise McError(
                    f"{MAX_MOUNTS} sequences are mounted or starting; unmount unused media first",
                    code="engine-busy",
                )
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise McError(f"Cannot create mount folder: {exc}", code="output-unwritable") from None
            if any(path.iterdir()):
                raise McError("Mount folder contains files; refusing to hide them", code="output-exists")
            previous = self.processes.pop(identity, None)
            save_record(folder, mount)
            temporary = folder / "starting.part"
            temporary.write_text(json.dumps({"pid": os.getpid(), "start": process_start(os.getpid())}))
            temporary.replace(folder / "starting.json")
            (folder / "ready.json").unlink(missing_ok=True)
        try:
            if previous is not None:
                try:
                    previous.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with self._locked():
                        self.processes[identity] = previous
                    raise McError(
                        "The previous mount service is still stopping; try restoring again",
                        code="engine-busy",
                    ) from None
            with work_slot(self.root):
                return self._launch(mount, folder)
        finally:
            (folder / "starting.json").unlink(missing_ok=True)

    def _launch(self, mount: MountedSequence, folder: Path) -> MountedSequence:
        from mcrawsome.mount.runtime import runtime_command

        with runtime_command() as command, (folder / "service.log").open("ab") as log:
            process = spawn(
                [*command, str(folder)],
                role="mount",
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        with self._locked():
            self.processes[mount.id] = process
        deadline = time.monotonic() + 10
        ready = folder / "ready.json"
        while process.poll() is None and time.monotonic() < deadline:
            if ready.exists() and is_mounted(mount.path):
                mount.frames = int(json.loads(ready.read_text())["frames"])
                mount.active = mount.serving = True
                mount.in_use = usage(folder)
                with self._locked():
                    save_record(folder, mount)
                return mount
            time.sleep(0.02)
        if is_mounted(mount.path):
            self._unmount(mount.path)
        if process.poll() is None:
            process.terminate()
            _signal_group(process, signal.SIGTERM)  # the service behind an AppImage launcher too
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()  # it ignored SIGTERM; listing() reaps it once it has exited
            _signal_group(process, signal.SIGKILL)
        else:
            with self._locked():
                self.processes.pop(mount.id, None)
        raise McError(f"Virtual mount failed; see {folder / 'service.log'}", code="input-unreadable")

    def _unmount(self, path: Path, *, dead: bool = False) -> None:
        """`dead`: its service has gone, so the files behind the mount point are unreadable already;
        detach it even while a program still holds them open (a lazy unmount, as a file manager's eject),
        which a normal unmount refuses as in use."""
        command = helper()
        if command is None:
            raise McError("fusermount3 is unavailable", code="input-unreadable")
        arguments = [command, "-u", "-z", str(path)] if dead else [command, "-u", str(path)]
        deadline = time.monotonic() + UNMOUNT_WAIT_S
        while True:
            try:
                result = subprocess.run(
                    arguments, capture_output=True, text=True, timeout=10, env=host_environment()
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise McError(f"Could not unmount: {exc}", code="engine-busy") from None
            # Open files hold a mount for as long as they are open; a passing lookup, for milliseconds.
            if not result.returncode or "busy" not in result.stderr.lower() or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if result.returncode:
            detail = result.stderr.strip()
            message = (
                "Media is in use; close its open files in the receiving application before unmounting"
                if "busy" in detail.lower()
                else f"Could not unmount: {detail}"
            )
            raise McError(message, code="engine-busy")
        # Also cover superseded mount roots: their record may be replaced on the next start.
        with self._locked():
            if not is_mounted(path) and not path.is_symlink():
                with suppress(OSError):
                    path.rmdir()

    def unmount(self, identity: str) -> MountedSequence:
        with self._identity_locked(identity):
            with self._locked():
                mount = self._read(identity)
            if mount.active:
                self._unmount(mount.path, dead=not mount.serving)
            with self._locked():
                process = self.processes.pop(identity, None)
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:  # unmounted but still exiting: listing() reaps it
                    with self._locked():
                        self.processes[identity] = process
            mount.active = mount.serving = mount.retained = False
            mount.in_use = None
            with self._locked():
                save_record(self.root / identity, mount)
                self._clean_mountpoint(mount)
        self.prune([identity])
        from mcrawsome.mount.runtime import collect_runtimes

        collect_runtimes()
        return mount
