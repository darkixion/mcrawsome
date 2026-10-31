"""The library watcher: file events are hints for a debounced rescan, whose changes are published."""

import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from mcrawsome.library.scan import Library, LibraryChange
from mcrawsome.library.watch import LibraryWatcher

STEP = 41_666_667


def _age(path: Path) -> None:
    when = time.time() - 3600
    os.utime(path, (when, when))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "clips"
    path.mkdir()
    return path


@pytest.fixture
def clip(make_mcraw: Callable[..., Path]) -> Callable[[Path, int], Path]:
    def make(path: Path, first: int = 0) -> Path:
        make_mcraw(path, [first + i * STEP for i in range(3)])
        _age(path)
        return path

    return make


class Running:
    """A watcher running on its own thread until the test ends."""

    def __init__(self, watcher: LibraryWatcher) -> None:
        self.watcher = watcher
        self.changes = watcher.subscribe()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=watcher.run, args=(self.stop,), daemon=True)

    def next(self, timeout: float) -> LibraryChange:
        return self.changes.get(timeout=timeout)

    def until(self, accept: Callable[[LibraryChange], bool], timeout: float) -> LibraryChange:
        deadline = time.monotonic() + timeout
        while True:
            change = self.next(max(0.0, deadline - time.monotonic()))
            if accept(change):
                return change


@pytest.fixture
def running(folder: Path) -> Iterator[Callable[..., Running]]:
    started: list[Running] = []

    def start(**options: Any) -> Running:
        run = Running(LibraryWatcher(Library(), lambda: [folder], **options))
        run.thread.start()
        started.append(run)
        return run

    yield start
    for run in started:
        run.stop.set()
        run.thread.join(10)
        assert not run.thread.is_alive()


def test_duplicate_events_publish_one_change(
    folder: Path, clip: Callable[[Path, int], Path], running: Callable[..., Running]
) -> None:
    target = folder / "a.mcraw"

    def events(*_paths: Path, **_options: Any) -> Iterator[set[tuple[int, str]]]:
        path = target if target.exists() else clip(target, 0)
        for _ in range(3):  # the same event, more than once
            yield {(1, str(path))}
        time.sleep(0.3)

    run = running(watch=events)
    change = run.next(timeout=10)
    assert [c.name for c in change.clips] == ["a.mcraw"]
    with pytest.raises(queue.Empty):
        run.next(timeout=1)


def test_a_new_clip_is_published_within_three_seconds(
    folder: Path, clip: Callable[[Path, int], Path], running: Callable[..., Running]
) -> None:
    run = running()
    time.sleep(0.5)  # the watch is in place
    added = time.monotonic()
    clip(folder / "a.mcraw", 0)
    change = run.until(lambda c: any(clip.name == "a.mcraw" for clip in c.clips), timeout=3)
    assert time.monotonic() - added < 3
    assert change.removed == []


def test_a_clip_written_to_a_temporary_name_is_published_under_its_own(
    folder: Path, clip: Callable[[Path, int], Path], running: Callable[..., Running]
) -> None:
    run = running()
    time.sleep(0.5)
    temporary = clip(folder / ".syncthing.a.mcraw.tmp", 0)
    temporary.rename(folder / "a.mcraw")
    change = run.until(lambda c: bool(c.clips), timeout=5)
    assert [c.name for c in change.clips] == ["a.mcraw"]


def test_a_folder_that_disappears_is_reported_and_watched_again_when_it_returns(
    folder: Path, clip: Callable[[Path, int], Path], running: Callable[..., Running], tmp_path: Path
) -> None:
    clip(folder / "a.mcraw", 0)
    run = running(recheck_s=0.5)
    first = run.until(lambda c: bool(c.clips), timeout=5)
    away = tmp_path / "unplugged"
    folder.rename(away)
    gone = run.until(lambda c: bool(c.missing_folders), timeout=5)
    assert gone.removed == [first.clips[0].id]
    assert gone.missing_folders == [folder]
    away.rename(folder)
    back = run.until(lambda c: bool(c.clips), timeout=5)
    assert back.clips[0].id == first.clips[0].id
    assert back.missing_folders == []
    clip(folder / "b.mcraw", 1)  # and its changes are seen again
    run.until(lambda c: any(clip.name == "b.mcraw" for clip in c.clips), timeout=5)


def test_a_renamed_clip_is_published_under_its_new_name_with_the_same_id(
    folder: Path, clip: Callable[[Path, int], Path], running: Callable[..., Running]
) -> None:
    clip(folder / "a.mcraw", 0)
    run = running()
    first = run.until(lambda c: bool(c.clips), timeout=5)
    (folder / "a.mcraw").rename(folder / "b.mcraw")
    change = run.until(lambda c: bool(c.clips), timeout=5)
    assert [(c.name, c.id) for c in change.clips] == [("b.mcraw", first.clips[0].id)]
    assert change.removed == []


def test_stopping_does_not_scan_again(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    calls: list[None] = []

    def events(*paths: Path, **options: Any) -> Iterator[set[tuple[int, str]]]:
        stop.set()
        yield {(1, "changed")}

    watcher = LibraryWatcher(Library(), lambda: [folder], watch=events)
    monkeypatch.setattr(watcher, "_refresh", lambda: calls.append(None))
    watcher.run(stop)
    assert len(calls) == 1


def test_slow_subscriber_keeps_a_bounded_queue(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = Library()
    watcher = LibraryWatcher(library, lambda: [folder])
    changes = watcher.subscribe()
    for i in range(1000):
        change = LibraryChange(clips=[], removed=[str(i)], removed_paths=[], missing_folders=[])
        monkeypatch.setattr(library, "refresh", lambda *args, change=change: (library.view, change))
        watcher.refresh()
    assert changes.qsize() <= 64
    latest = changes.get_nowait()
    while not changes.empty():
        latest = changes.get_nowait()
    assert latest.removed == ["999"]


def test_overflowing_a_subscriber_leaves_a_reload_marker_not_a_gap(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = Library()
    watcher = LibraryWatcher(library, lambda: [folder])
    changes = watcher.subscribe()
    for i in range(100):
        change = LibraryChange(clips=[], removed=[str(i)], removed_paths=[], missing_folders=[])
        monkeypatch.setattr(library, "refresh", lambda *args, change=change: (library.view, change))
        watcher.refresh()
    received = [changes.get_nowait() for _ in range(changes.qsize())]
    assert received[0].reload
    assert all(not change.reload for change in received[1:])
    # The marker covers everything dropped before it; the latest change still arrives after it.
    assert received[-1].removed == ["99"]
    assert len(received) <= 64


def test_watch_registers_only_scanned_levels(folder: Path) -> None:
    child = folder / "child"
    child.mkdir()
    (child / "deep").mkdir()
    for quiet in (".gallery", "CacheClip", "ProxyMedia", "Resolve Project Backups"):  # Resolve writes there
        (folder / quiet).mkdir()
    stop = threading.Event()
    registered: list[tuple[tuple[Path, ...], dict[str, Any]]] = []

    def events(*paths: Path, **options: Any) -> Iterator[set[tuple[int, str]]]:
        registered.append((paths, options))
        stop.set()
        yield set()

    LibraryWatcher(Library(), lambda: [folder], watch=events).run(stop)
    paths, options = registered[0]
    assert options.get("recursive") is False
    assert set(paths) == {folder, child}


def test_clips_still_waiting_for_their_movement_are_scanned_again(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    library = Library()
    library.pending_motion = True
    calls: list[None] = []

    def events(*paths: Path, **options: Any) -> Iterator[set[tuple[int, str]]]:
        yield set()  # the first scan once the watch is in place
        yield set()  # a timeout with no file events
        stop.set()
        yield set()

    watcher = LibraryWatcher(library, lambda: [folder], watch=events)
    monkeypatch.setattr(watcher, "_refresh", lambda: calls.append(None))
    watcher.run(stop)
    assert len(calls) >= 3  # the start, the first scan and the timeout's rescan for pending movement
