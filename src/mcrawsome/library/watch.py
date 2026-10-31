"""The library watcher: file events in the library folders lead to a rescan, and its changes are published.

Events are only hints. Changes are gathered for up to a second (until 0.2 s pass with none), then the
folders are scanned again and compared with the last scan; only what differs is published, so repeated
events, temporary files and renames publish what the folders now hold, once. While a clip is copying or
a folder is missing (an unplugged drive), the folders are also scanned every `recheck_s` seconds, since
a file that stops changing, or a folder that comes back, sends no event that is watched; so are they
while clips' movement is still to be measured (each scan measures for about a second).
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import watchfiles

from mcrawsome.library.formats import scanned_folder
from mcrawsome.library.scan import Library, LibraryChange, LibraryView

DEBOUNCE_MS = 1000  # changes are gathered for at most this long before a rescan
QUIET_MS = 200  # or until this long passes with no new change
RECHECK_S = 2.0

log = logging.getLogger(__name__)

Changes = queue.Queue[LibraryChange]
MAX_PENDING = 64
Watch = Callable[..., Iterator[set[tuple[Any, str]]]]


def _reset(subscriber: Changes, change: LibraryChange) -> None:
    """Replace everything pending with one marker telling the client to read the library again."""
    while True:
        try:
            subscriber.get_nowait()
        except queue.Empty:
            break
    subscriber.put_nowait(
        LibraryChange(
            clips=[], removed=[], removed_paths=[], missing_folders=change.missing_folders, reload=True
        )
    )


class LibraryWatcher:
    """Scans `folders()` (listing files whose real path is in `roots()`, by default the folders) and
    publishes each change to every subscriber."""

    def __init__(
        self,
        library: Library,
        folders: Callable[[], list[Path]],
        roots: Callable[[], list[Path]] | None = None,
        watch: Watch = watchfiles.watch,
        recheck_s: float = RECHECK_S,
    ) -> None:
        self.library = library
        self.folders = folders
        self.roots = roots
        self.watch = watch
        self.recheck_s = recheck_s
        self._subscribers: set[Changes] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> Changes:
        """A bounded queue of changes. A slow client that falls behind gets its queue cleared and a `reload`
        change instead, because dropping one delta would lose its clips and removals for good."""
        changes: Changes = queue.Queue(maxsize=MAX_PENDING)
        with self._lock:
            self._subscribers.add(changes)
        return changes

    def unsubscribe(self, changes: Changes) -> None:
        with self._lock:
            self._subscribers.discard(changes)

    def refresh(self) -> LibraryView:
        """Scan now; a change is published."""
        view, change = self.library.refresh(self.folders(), self.roots() if self.roots is not None else None)
        if change is not None:
            with self._lock:
                for subscriber in self._subscribers:
                    try:
                        subscriber.put_nowait(change)
                    except queue.Full:
                        _reset(subscriber, change)
        return view

    def run(self, stop: threading.Event) -> None:
        """Watch the folders until `stop` is set (run on a thread of its own)."""
        if not stop.is_set():
            self._refresh()
        while not stop.is_set():
            watched = self._present()
            if not watched:  # nothing to watch: look again later
                if not stop.wait(self.recheck_s):
                    self._refresh()
                continue
            try:
                self._follow(watched, stop)
            except Exception:  # a folder going while it is watched, say: start again
                log.exception("watching the library folders failed")
                stop.wait(self.recheck_s)
            if not stop.is_set():
                self._refresh()

    def _follow(self, watched: list[Path], stop: threading.Event) -> None:
        """Rescan on the folders' events until the folders to watch change."""
        events = self.watch(
            *watched,
            recursive=False,
            stop_event=stop,
            debounce=DEBOUNCE_MS,
            step=QUIET_MS,
            rust_timeout=round(self.recheck_s * 1000),
            yield_on_timeout=True,
            ignore_permission_denied=True,
        )
        first = True  # the watch is now in place: a change made before it is found by one rescan
        for changes in events:
            if stop.is_set():
                return
            view = self.library.view
            waiting = (
                any(clip.status == "copying" for clip in view.clips)
                or view.missing_folders
                or self.library.pending_motion  # movement still to measure for some clips
            )
            if changes or waiting or first:
                first = False
                self._refresh()
            if self._present() != watched:
                return

    def _present(self) -> list[Path]:
        watched: set[Path] = set()
        for folder in self.folders():
            if folder.is_dir():
                watched.add(folder)
                with contextlib.suppress(OSError):  # the subfolders the scan reads: Resolve's own stay quiet
                    watched.update(
                        path for path in folder.iterdir() if scanned_folder(path) and path.is_dir()
                    )
        return sorted(watched)

    def _refresh(self) -> None:
        try:
            self.refresh()
        except Exception:
            log.exception("scanning the library folders failed")
