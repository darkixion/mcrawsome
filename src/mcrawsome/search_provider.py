"""Find recordings from GNOME's search: a session D-Bus service that GNOME Shell starts when it searches.

It answers from the library MCRAWsome last scanned (`state_dir()/library.json`) and each clip's details,
matching the file name and the words the Library's own search matches, so the engine need not be
running. It quits once GNOME has not asked anything for a minute.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcrawsome.appimage import launcher
from mcrawsome.library.annotations import details_for_search, search_text
from mcrawsome.paths import state_dir
from mcrawsome.processes import spawn

BUS_NAME = "io.github.darkixion.mcrawsome.SearchProvider"
OBJECT_PATH = "/io/github/darkixion/mcrawsome/SearchProvider"
IDLE_S = 60  # D-Bus starts the service again for a later search
LIMIT = 50  # results offered for one search; GNOME shows the first few
INTERFACE = """
<node>
  <interface name="org.gnome.Shell.SearchProvider2">
    <method name="GetInitialResultSet">
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetSubsearchResultSet">
      <arg type="as" name="previous_results" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetResultMetas">
      <arg type="as" name="identifiers" direction="in"/>
      <arg type="aa{sv}" name="metas" direction="out"/>
    </method>
    <method name="ActivateResult">
      <arg type="s" name="identifier" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="LaunchSearch">
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
  </interface>
</node>
"""


@dataclass(frozen=True)
class Recording:
    path: str
    title: str
    words: str  # what a search matches, case-folded
    captured: str | None
    duration_s: float | None
    camera: str | None


def recordings(record: Path) -> list[Recording]:
    """The library's recordings that are present, newest first."""
    try:
        files = json.loads(record.read_text())["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    found = []
    for path, facts in files.items() if isinstance(files, dict) else ():
        if not isinstance(facts, dict) or not isinstance(facts.get("id"), str) or not os.path.isfile(path):
            continue
        source = Path(path)
        details = details_for_search(source, facts["id"])
        words = [source.name, search_text(details) if details is not None else ""]
        captured, duration, camera = facts.get("captured"), facts.get("duration_s"), facts.get("camera")
        found.append(
            Recording(
                path=path,
                title=details.title if details is not None and details.title else source.name,
                words="\n".join(words).casefold(),
                captured=captured if isinstance(captured, str) else None,
                duration_s=float(duration) if isinstance(duration, int | float) else None,
                camera=camera if isinstance(camera, str) else None,
            )
        )
    found.sort(key=lambda item: (item.captured or "", item.path), reverse=True)
    return found


def thumbnail(path: str) -> str | None:
    """The file manager's thumbnail of a recording, by the freedesktop thumbnail rules, if it made one."""
    uri = "file://" + quote(path, safe="/!$&'()*+,;=:@~")  # escaped as GLib escapes file URIs
    name = hashlib.md5(uri.encode(), usedforsecurity=False).hexdigest() + ".png"  # named by MD5, by the rules
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "thumbnails"
    for size in ("xx-large", "x-large", "large", "normal"):
        if (cache / size / name).is_file():
            return str(cache / size / name)
    return None


def describe(recording: Recording) -> dict[str, str]:
    """GNOME's result: the clip's title, its length, date and phone, and its thumbnail."""
    parts = []
    if recording.duration_s:
        seconds = round(recording.duration_s)
        hours, minutes = divmod(seconds // 60, 60)
        clock = f"{minutes:02d}:{seconds % 60:02d}" if hours else f"{minutes}:{seconds % 60:02d}"
        parts.append(f"{hours}:{clock}" if hours else clock)
    if recording.captured:
        try:
            when = datetime.fromisoformat(recording.captured).astimezone()
            parts.append(f"{when.day} {when:%B %Y}")
        except ValueError:
            pass
    if recording.camera:
        parts.append(recording.camera)
    return {
        "id": recording.path,
        "name": recording.title,
        "description": ", ".join(parts),
        "gicon": thumbnail(recording.path) or "video-x-generic",  # a path, or an icon's name
    }


def open_window(*arguments: str) -> None:
    """Start the window in a unit of its own: D-Bus ends this service's unit, and all in it, when it
    quits."""
    image = launcher()
    program = [image] if image else [sys.executable, "-m", "mcrawsome.cli.main"]
    spawn([*program, "gui", *arguments], role="window")


class Search:
    """Answers GNOME Shell's searches, reading the library again whenever it changes."""

    def __init__(self, record: Path | None = None) -> None:
        self.record = record or state_dir() / "library.json"
        self.stamp: tuple[int, int] | None = None
        self.found: list[Recording] = []

    def current(self) -> list[Recording]:
        try:
            status = self.record.stat()
            stamp: tuple[int, int] | None = (status.st_size, status.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp != self.stamp:
            self.stamp, self.found = stamp, recordings(self.record)
        return self.found

    def results(self, terms: list[str], within: list[str] | None = None) -> list[str]:
        words = [term.casefold() for term in terms if term.strip()]
        if not words:
            return []
        matching = [item.path for item in self.current() if all(word in item.words for word in words)]
        if within is not None:  # narrowing a search as more is typed keeps GNOME's order
            kept = set(matching)
            return [path for path in within if path in kept][:LIMIT]
        return matching[:LIMIT]

    def metas(self, identifiers: list[str]) -> list[dict[str, str]]:
        known = {item.path: item for item in self.current()}
        return [describe(known[path]) for path in identifiers if path in known]

    def activate(self, identifier: str) -> None:
        if identifier in {item.path for item in self.current()}:
            open_window("--", identifier)

    def launch(self) -> None:
        open_window("--view", "library")


def serve(search: Search | None = None) -> None:
    """Own the bus name and answer GNOME Shell until it has asked nothing for IDLE_S seconds."""
    gio = importlib.import_module("gi.repository.Gio")
    glib = importlib.import_module("gi.repository.GLib")

    search = search or Search()
    interface = gio.DBusNodeInfo.new_for_xml(INTERFACE).interfaces[0]
    loop = glib.MainLoop()
    idle = [glib.timeout_add_seconds(IDLE_S, loop.quit)]

    def called(
        connection: Any, sender: str, path: str, name: str, method: str, parameters: Any, invocation: Any
    ) -> None:
        glib.source_remove(idle[0])
        idle[0] = glib.timeout_add_seconds(IDLE_S, loop.quit)
        arguments = parameters.unpack()
        try:
            if method == "GetInitialResultSet":
                invocation.return_value(glib.Variant("(as)", (search.results(arguments[0]),)))
            elif method == "GetSubsearchResultSet":
                invocation.return_value(glib.Variant("(as)", (search.results(arguments[1], arguments[0]),)))
            elif method == "GetResultMetas":
                metas = [
                    {key: glib.Variant("s", value) for key, value in meta.items()}
                    for meta in search.metas(arguments[0])
                ]
                invocation.return_value(glib.Variant("(aa{sv})", (metas,)))
            elif method == "ActivateResult":
                search.activate(arguments[0])
                invocation.return_value(None)
            elif method == "LaunchSearch":
                search.launch()
                invocation.return_value(None)
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as exc:  # a failed search must not end the service
            invocation.return_dbus_error("org.gnome.Shell.SearchProvider2.Error.Failed", str(exc))

    def acquired(connection: Any, name: str) -> None:
        connection.register_object(OBJECT_PATH, interface, called, None, None)

    gio.bus_own_name(
        gio.BusType.SESSION, BUS_NAME, gio.BusNameOwnerFlags.NONE, acquired, None, lambda *lost: loop.quit()
    )
    loop.run()
