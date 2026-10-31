"""MotionCam recordings' details as list-view columns, and emblems on recordings mounted for DaVinci
Resolve. Details are read by a child process, a folder's worth at a time, keeping Files' main loop
responsive; the emblems come from MCRAWsome's own mount records, without asking its engine."""

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

COLUMNS = (
    ("duration", "Duration", "How long a MotionCam recording runs"),
    ("resolution", "Resolution", "A MotionCam recording's frame size"),
    ("frame_rate", "Frame rate", "A MotionCam recording's frame rate"),
    ("camera", "Camera", "The phone that made a MotionCam recording"),
)
EMBLEMS = {"mounted": "mcrawsome-mounted", "in-use": "mcrawsome-in-use"}  # icons emblem-mcrawsome-*
BATCH = 64  # recordings one child process reads
TIMEOUT_S = 60  # a child process still reading after this long is stopped
GATHER_MS = 100  # Files asks for a folder's files one by one: wait this long to read them together
REMEMBERED = 4096  # recordings whose details and emblems are kept
MOUNTINFO = Path("/proc/self/mountinfo")


def local_recording(file):
    """The local path of a .mcraw file, else None."""
    uri = urlsplit(file.get_uri())
    path = unquote(uri.path)
    if (
        uri.scheme != "file"
        or uri.netloc not in ("", "localhost")
        or not path.startswith("/")
        or "\0" in path
        or Path(path).suffix.lower() != ".mcraw"
        or file.is_directory()
    ):
        return None
    return path


def state_dir():
    """MCRAWsome's state folder, as the engine finds it."""
    if os.environ.get("MCRAWSOME_STATE_DIR"):
        return Path(os.environ["MCRAWSOME_STATE_DIR"])
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local/state") / "mcrawsome"


def started(pid):
    """A process's start time, which tells it apart from a later one given the same id."""
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def mount_points():
    points = set()
    try:
        lines = MOUNTINFO.read_text().splitlines()
    except OSError:
        return points
    for line in lines:
        if " - fuse.mcrawsome " in line:
            point = line.split()[4]
            for escaped, character in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
                point = point.replace(escaped, character)
            points.add(point)
    return points


def mounted_recordings():
    """Each mounted recording's real path and state: "in-use" while an application has its media open,
    else "mounted"."""
    points = mount_points()
    states = {}
    if not points:
        return states
    try:
        folders = list((state_dir() / "mounts").iterdir())
    except OSError:
        return states
    for folder in folders:
        try:
            record = json.loads((folder / "sequence.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("path") not in points:
            continue
        if not isinstance(record.get("source"), str):
            continue
        state = "mounted"
        try:
            usage = json.loads((folder / "usage.json").read_text())
            if usage.get("in_use") is True and started(usage.get("pid")) == usage.get("start"):
                state = "in-use"
        except (OSError, ValueError, AttributeError):
            pass
        source = os.path.realpath(record["source"])
        if states.get(source) != "in-use":  # a recording mounted twice is in use if either mount is
            states[source] = state
    return states


def key(path):
    """A recording's details last as long as its size and modification time."""
    try:
        status = os.stat(path)
    except OSError:
        return (path, None, None)
    return (path, status.st_size, status.st_mtime_ns)


def show(file, columns):
    for name, _label, _description in COLUMNS:
        value = columns.get(name)
        file.add_string_attribute(f"mcrawsome_{name}", value if isinstance(value, str) else "")


class MCRAWsomeColumns(GObject.GObject, Nautilus.ColumnProvider, Nautilus.InfoProvider):
    def __init__(self):
        super().__init__()
        self.read = {}  # key(path): columns
        self.waiting = {}  # path: [(provider, handle, closure, file)]
        self.queued = []  # paths for the next child process
        self.reading = False
        self.gathering = False
        self.files = {}  # real path: the file Files last showed, for emblem changes
        self.states = mounted_recordings()
        GLib.timeout_add_seconds(3, self.watch)

    def get_columns(self):
        return [
            Nautilus.Column(
                name=f"MCRAWsome::{name}", attribute=f"mcrawsome_{name}", label=label, description=description
            )
            for name, label, description in COLUMNS
        ]

    def update_file_info_full(self, provider, handle, closure, file):
        path = local_recording(file)
        if path is None:
            return Nautilus.OperationResult.COMPLETE
        real = os.path.realpath(path)
        self.files.pop(real, None)
        self.files[real] = file
        while len(self.files) > REMEMBERED:
            self.files.pop(next(iter(self.files)))
        state = self.states.get(real)
        if state is not None:
            file.add_emblem(EMBLEMS[state])
        known = self.read.get(key(path))
        if known is not None:
            show(file, known)
            return Nautilus.OperationResult.COMPLETE
        self.waiting.setdefault(path, []).append((provider, handle, closure, file))
        if path not in self.queued:
            self.queued.append(path)
        if not self.gathering and not self.reading:
            self.gathering = True
            GLib.timeout_add(GATHER_MS, self.gathered)
        return Nautilus.OperationResult.IN_PROGRESS

    def cancel_update(self, provider, handle):
        for path, waiters in list(self.waiting.items()):
            kept = [waiter for waiter in waiters if waiter[1] is not handle]
            if kept:
                self.waiting[path] = kept
            else:
                del self.waiting[path]

    def gathered(self):
        self.gathering = False
        self.start()
        return False

    def start(self):
        if self.reading or not self.queued:
            return
        batch, self.queued = self.queued[:BATCH], self.queued[BATCH:]
        self.reading = True
        try:
            process = Gio.Subprocess.new(
                ["/usr/bin/mcrawsome", "file-columns", "--", *batch],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except Exception:  # GLib.Error: MCRAWsome is not installed where Files expects it
            self.finish(batch, {})
            return
        done = False

        def timeout():
            nonlocal done
            if not done:
                done = True
                process.force_exit()
                self.finish(batch, {})
            return False

        timer = GLib.timeout_add_seconds(TIMEOUT_S, timeout)

        def finished(child, result, data):
            nonlocal done
            if done:
                return
            done = True
            GLib.source_remove(timer)
            try:
                _, output, _ = child.communicate_utf8_finish(result)
                found = json.loads(output) if child.get_successful() else {}
            except Exception:  # an unreadable answer leaves the columns empty
                found = {}
            self.finish(batch, found if isinstance(found, dict) else {})

        process.communicate_utf8_async(None, None, finished, None)

    def finish(self, batch, found):
        for path in batch:
            columns = found.get(path)
            columns = columns if isinstance(columns, dict) else {}
            self.read[key(path)] = columns
            while len(self.read) > REMEMBERED:
                self.read.pop(next(iter(self.read)))
            for provider, handle, closure, file in self.waiting.pop(path, []):
                show(file, columns)
                Nautilus.info_provider_update_complete_invoke(
                    closure, provider, handle, Nautilus.OperationResult.COMPLETE
                )
        self.reading = False
        self.start()

    def watch(self):
        """Refresh the files whose recording was mounted, unmounted, or opened or closed by Resolve."""
        if not self.files:
            return True
        states = mounted_recordings()
        paths = states.keys() | self.states.keys()
        changed = {path for path in paths if states.get(path) != self.states.get(path)}
        self.states = states
        for path in changed:
            file = self.files.get(path)
            if file is not None:
                file.invalidate_extension_info()
        return True
