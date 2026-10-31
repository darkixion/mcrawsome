import errno
import json
import mmap
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from mcrawsome.errors import McError
from mcrawsome.mount.manager import MountManager
from mcrawsome.paths import state_dir


@pytest.mark.parametrize("captured", [False, True])
def test_auxiliary_mount_can_preserve_captured_remosaic_mode_after_preferences_change(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch, captured: bool
) -> None:
    from mcrawsome.server.settings import InterfaceSettings

    current = InterfaceSettings(quad_bayer_remosaic=not captured)
    monkeypatch.setattr("mcrawsome.server.settings.load_settings", lambda: (current, None))
    manager = MountManager()
    available = manager.listing()
    if not available.available:
        pytest.skip(available.reason)
    try:
        explicit = manager.mount(tiny_raw, quad_bayer_remosaic=captured)
        inherited = manager.mount(tiny_raw)
        assert explicit.quad_bayer_remosaic is captured
        assert inherited.quad_bayer_remosaic is not captured
        assert explicit.id != inherited.id
        assert MountManager().get(explicit.id).quad_bayer_remosaic is captured
        current.quad_bayer_remosaic = captured
        assert manager.mount(tiny_raw).id == explicit.id
    finally:
        for mount in manager.listing().mounts:
            if mount.active:
                manager.unmount(mount.id)


def test_distinct_mounts_can_start_together_while_the_first_service_is_starting(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    second = Path(shutil.copyfile(tiny_raw, tmp_path / "second.mcraw"))
    original = subprocess.Popen
    second_started = threading.Event()
    guard = threading.Lock()
    calls = 0
    overlapped = []

    def launch(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        process = original(*args, **kwargs)
        with guard:
            calls += 1
            first = calls == 1
            if calls == 2:
                second_started.set()
        if first:
            overlapped.append(second_started.wait(2))
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            values = list(workers.map(manager.mount, [tiny_raw, second]))
        assert all(mount.serving for mount in values)
        assert overlapped == [True], "a global lock serialises service startup"
    finally:
        for mount in manager.listing().mounts:
            if mount.active:
                manager.unmount(mount.id)


def test_concurrent_requests_for_the_same_source_reuse_one_service(tiny_raw: Path) -> None:
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            mounts = list(workers.map(manager.mount, [tiny_raw, tiny_raw]))
        assert mounts[0].id == mounts[1].id
        assert len(manager.processes) == 1
        assert all(mount.serving for mount in mounts)
    finally:
        for mount in manager.listing().mounts:
            if mount.active:
                manager.unmount(mount.id)


def test_starting_mount_reserves_capacity_without_blocking_listing(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    import mcrawsome.mount.manager as module

    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    other = Path(shutil.copyfile(tiny_raw, tmp_path / "other.mcraw"))
    entered = threading.Event()
    release = threading.Event()
    original = manager._launch

    def launch(*args, **kwargs):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "MAX_MOUNTS", 1)
    monkeypatch.setattr(manager, "_launch", launch)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(manager.mount, tiny_raw)
            try:
                assert entered.wait(2)
                assert len(manager.listing().mounts) == 1
                with pytest.raises(McError, match="mounted or starting"):
                    manager.mount(other)
            finally:
                release.set()
            assert pending.result().serving
    finally:
        for mount in manager.listing().mounts:
            if mount.active:
                manager.unmount(mount.id)
    assert not list(manager.root.glob("*/starting.json"))


@pytest.mark.parametrize("modified_ns", [1_790_000_000_123_456_789, -1_000_000_001])
def test_virtual_files_keep_source_modification_time_across_remounts(
    tiny_raw: Path, modified_ns: int
) -> None:
    os.utime(tiny_raw, ns=(modified_ns, modified_ns))
    assert tiny_raw.stat().st_mtime_ns == modified_ns
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    for attempt in range(2):
        if attempt:
            mounted = manager.mount(tiny_raw)
        try:
            assert mounted.path.stat().st_mtime_ns == modified_ns
            media = list(mounted.path.iterdir())
            assert len(media) == 4
            assert all(path.stat().st_mtime_ns == modified_ns for path in media)
            assert (mounted.path / "tiny-000000.dng").read_bytes()[:4] == b"II*\0"
        finally:
            manager.unmount(mounted.id)


def test_real_mount_exposes_read_only_dng_and_unmounts(tiny_raw: Path) -> None:
    manager = MountManager()
    status = manager.listing()
    if not status.available:
        pytest.skip(status.reason)
    mounted = manager.mount(tiny_raw)
    try:
        assert mounted.active
        assert mounted.path.is_mount()
        assert sorted(p.suffix for p in mounted.path.iterdir()) == [".dng", ".dng", ".dng", ".wav"]
        assert (mounted.path / "tiny-000002.dng").read_bytes()[:4] == b"II*\0"
        with (
            (mounted.path / "tiny-000002.dng").open("rb") as file,
            mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapped,
        ):
            assert mapped[:4] == b"II*\0"
        with pytest.raises(OSError, match="Read-only file system"):
            (mounted.path / "new.txt").write_text("no")
        assert manager.mount(tiny_raw).id == mounted.id
        assert len(manager.listing().mounts) == 1
        # Another manager sees the same mount, including after a window or engine reconnect.
        assert MountManager().get(mounted.id).path == mounted.path
    finally:
        manager.unmount(mounted.id)
    assert not mounted.path.is_mount()
    assert manager.listing().mounts == []
    history = (state_dir() / "mount-history.log").read_text().splitlines()
    assert json.loads(history[-1])["id"] == mounted.id


def test_mounting_again_after_unmount_keeps_paths_and_the_record_is_gone(tiny_raw: Path) -> None:
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    manager.unmount(mounted.id)
    assert not (manager.root / mounted.id).exists()
    with pytest.raises(McError, match="No such mounted sequence"):
        manager.remount(mounted.id)
    again = manager.mount(tiny_raw)
    try:
        assert again.id == mounted.id
        assert again.path == mounted.path
    finally:
        manager.unmount(again.id)


def test_explicit_remount_restarts_idle_service(tiny_raw: Path) -> None:
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    try:
        manager.retain([mounted.id])
        first = manager.processes[mounted.id]
        again = manager.remount(mounted.id)
        assert again.path == mounted.path
        assert again.retained
        assert again.sent
        assert first.poll() == 0
        assert manager.processes[mounted.id].pid != first.pid
        assert (again.path / "tiny-000000.dng").read_bytes()[:4] == b"II*\0"
        with (again.path / "tiny-000000.dng").open("rb") as opened:
            opened.read(8)
            with pytest.raises(McError, match="in use"):
                manager.remount(mounted.id)
            assert manager.get(mounted.id).serving
    finally:
        manager.unmount(mounted.id)


def test_a_new_mount_recovers_unreadable_media_even_when_the_service_looks_alive(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcrawsome.mount.readiness as readiness

    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    first = manager.processes[mounted.id]
    original = readiness.readable_media
    calls = []

    def unreadable_once(mount):  # type: ignore[no-untyped-def]
        calls.append(mount.id)
        if len(calls) == 1:
            raise ValueError("Virtual media is not readable")
        return original(mount)

    monkeypatch.setattr(readiness, "readable_media", unreadable_once)
    try:
        assert manager.get(mounted.id).serving
        again = manager.mount(tiny_raw)
        assert again.path == mounted.path
        assert manager.processes[mounted.id].pid != first.pid
        assert first.poll() == 0
        assert original(again) > 8
        # A real open editor handle must still prevent a recovery attempt.
        calls.clear()
        with (again.path / "tiny-000000.dng").open("rb") as opened:
            opened.read(8)
            with pytest.raises(McError, match="in use"):
                manager.mount(tiny_raw)
            assert manager.get(mounted.id).serving
    finally:
        manager.unmount(mounted.id)


def test_dead_service_is_visible_and_can_be_remounted(tiny_raw: Path) -> None:
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    try:
        worker = manager.processes[mounted.id]
        worker.terminate()
        worker.wait(timeout=5)
        assert not manager.get(mounted.id).serving
        again = manager.remount(mounted.id)
        assert again.serving
        assert (again.path / "tiny-000000.dng").read_bytes()[:4] == b"II*\0"
    finally:
        manager.unmount(mounted.id)


def test_new_send_recovers_a_disconnected_mount_at_its_existing_path(tiny_raw: Path) -> None:
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    try:
        manager.retain([mounted.id])
        worker = manager.processes[mounted.id]
        worker.kill()  # A hard stop leaves the disconnected FUSE mount behind.
        worker.wait(timeout=5)
        assert manager.get(mounted.id).active
        assert not manager.get(mounted.id).serving
        restored = manager.mount(tiny_raw)
        assert restored.id == mounted.id
        assert restored.path == mounted.path
        assert restored.serving
        assert restored.sent
        assert restored.retained
        assert (restored.path / "tiny-000000.dng").read_bytes()[:4] == b"II*\0"
    finally:
        manager.unmount(mounted.id)


def test_new_send_does_not_disconnect_a_live_service(tiny_raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(manager, "_unmount", lambda *_, **__: pytest.fail("live service was disconnected"))
            again = manager.mount(tiny_raw)
        assert again.id == mounted.id
        assert again.serving
    finally:
        manager.unmount(mounted.id)


def test_recovery_detaches_a_dead_mount_an_editor_still_holds(tiny_raw: Path) -> None:
    """The service has gone, so the held file is unreadable already; sending again detaches the old
    mount lazily and serves the recording afresh instead of refusing it as in use."""
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    try:
        handle = (mounted.path / "tiny-000000.dng").open("rb")
        try:
            worker = manager.processes[mounted.id]
            worker.kill()
            worker.wait(timeout=5)
            again = manager.mount(tiny_raw)
            assert again.id == mounted.id
            assert again.serving
            assert (again.path / "tiny-000000.dng").read_bytes()[:4] in (b"II*\0", b"MM\0*")
        finally:
            try:
                handle.close()
            except OSError as exc:
                if exc.errno != errno.ENOTCONN:
                    raise
    finally:
        manager.unmount(mounted.id)


def test_real_header_reads_do_not_touch_bad_sensor_payload(tiny_raw: Path, tmp_path: Path) -> None:
    import json

    from mcrawsome.io.container import ContainerWriter, read_frame_record, read_index
    from mcrawsome.output.dng import read_dng_tags

    path = tmp_path / "metadata_only.mcraw"
    with tiny_raw.open("rb") as file:
        index = read_index(file)
        payload, metadata = read_frame_record(file, index.frames[0].offset)
    frame = json.loads(metadata) | {"compressionType": 999}
    with ContainerWriter(path, index.metadata) as writer:
        writer.add_frame(0, payload, json.dumps(frame).encode())
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(path)
    try:
        frame_path = mounted.path / "metadata_only-000000.dng"
        assert read_dng_tags(frame_path)[256] == (128,)
        with pytest.raises(OSError, match="Input/output error"):
            frame_path.read_bytes()
    finally:
        manager.unmount(mounted.id)


@pytest.mark.parametrize("kind", ["file", "mapped", "directory"])
def test_live_usage_tracks_open_handles_and_prevents_unmount(tiny_raw: Path, kind: str) -> None:
    import time

    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mount = manager.mount(tiny_raw)

    def wait_usage(expected: bool) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if manager.get(mount.id).in_use is expected:
                return
            time.sleep(0.02)
        assert manager.get(mount.id).in_use is expected

    handle: int | mmap.mmap | BinaryIO | None = None
    try:
        wait_usage(False)
        if kind == "directory":
            handle = os.open(mount.path, os.O_RDONLY | os.O_DIRECTORY)
        elif kind == "mapped":
            with (mount.path / "tiny-000000.dng").open("rb") as stream:
                handle = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        else:
            handle = (mount.path / "tiny-000000.dng").open("rb")
        wait_usage(True)
        with pytest.raises(McError, match="in use"):
            manager.unmount(mount.id)
        assert manager.get(mount.id).active
    finally:
        if isinstance(handle, int):
            os.close(handle)
        elif handle is not None:
            handle.close()
        try:
            wait_usage(False)
        finally:
            manager.unmount(mount.id)


def test_missing_or_stale_usage_is_unknown_instead_of_idle(tmp_path: Path) -> None:
    import json

    from mcrawsome.mount.manager import process_start, usage

    folder = tmp_path / "record"
    folder.mkdir()
    assert usage(folder) is None
    current = {"pid": os.getpid(), "start": process_start(os.getpid())}
    (folder / "ready.json").write_text(json.dumps(current))
    (folder / "usage.json").write_text(json.dumps(current | {"in_use": True}))
    assert usage(folder) is True
    (folder / "usage.json").write_text(json.dumps(current | {"in_use": False}))
    assert usage(folder) is False
    (folder / "usage.json").write_text(json.dumps(current | {"start": "stale", "in_use": False}))
    assert usage(folder) is None
    (folder / "usage.json").write_text("invalid")
    assert usage(folder) is None


class _SlowProcess:
    """A service that is still exiting after five seconds, until it is killed."""

    pid = 4_194_305  # above Linux's largest process ID: no process group to signal

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self.killed:
            return -9
        raise subprocess.TimeoutExpired("mcrawsome.mount.service", timeout or 0)


def test_unmounting_saves_the_record_while_the_service_is_still_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import manager as module
    from mcrawsome.mount.manager import MountedSequence, save_record

    manager = MountManager()
    identity = "0123456789abcdef01234567"
    folder = manager.root / identity
    folder.mkdir(parents=True)
    record = MountedSequence(
        id=identity, source=tmp_path / "gone.mcraw", path=folder / "media", stamp=(1, 2, 3, 4),
        active=True, sent=True, retained=True,
    )  # fmt: skip
    save_record(folder, record)
    slow = _SlowProcess()
    manager.processes[identity] = slow  # type: ignore[assignment]
    monkeypatch.setattr(module, "is_mounted", lambda path: True)
    monkeypatch.setattr(manager, "_unmount", lambda path, dead=False: None)
    result = manager.unmount(identity)
    assert not result.retained
    saved = MountedSequence.model_validate_json((folder / "sequence.json").read_bytes())
    assert (saved.retained, saved.sent) == (False, True)
    assert manager.processes[identity] is slow  # type: ignore[comparison-overlap]  # reaped by listing()


def test_a_failed_start_kills_a_service_that_ignores_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import manager as module
    from mcrawsome.mount.manager import MountedSequence

    manager = MountManager()
    identity = "89abcdef0123456789abcdef"
    folder = manager.root / identity
    folder.mkdir(parents=True)
    slow = _SlowProcess()
    clock = iter([0.0, 100.0])
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: slow)
    monkeypatch.setattr("mcrawsome.mount.manager.time.monotonic", lambda: next(clock, 100.0))
    monkeypatch.setattr(module, "is_mounted", lambda path: False)
    mount = MountedSequence(
        id=identity, source=tmp_path / "a.mcraw", path=folder / "media", stamp=(1, 2, 3, 4)
    )
    with pytest.raises(McError, match="Virtual mount failed"):
        manager._launch(mount, folder)
    assert slow.killed


def test_baked_and_untouched_mounts_of_a_clip_are_different_media(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import manager as module
    from mcrawsome.recipe.raw import RawCalibration

    manager = MountManager()
    monkeypatch.setattr(module, "availability", lambda: (True, "available"))
    monkeypatch.setattr(manager, "_start", lambda mount: mount)
    plain = manager.mount(tiny_raw)
    baked = manager.mount(tiny_raw, raw_settings=RawCalibration(mount_shading="baked"))
    assert plain.id != baked.id
    assert plain.path != baked.path
    assert baked.raw_settings.mount_shading == "baked"


def test_a_marked_range_is_its_own_media(tiny_raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.mount import manager as module

    manager = MountManager()
    monkeypatch.setattr(module, "availability", lambda: (True, "available"))
    monkeypatch.setattr(manager, "_start", lambda mount: mount)
    whole, marked = manager.mount(tiny_raw), manager.mount(tiny_raw, frame_range=(1, 2))
    assert whole.id != marked.id
    assert (whole.frame_range, marked.frame_range) == (None, (1, 2))
    assert manager.mount(tiny_raw, frame_range=(1, 2)).id == marked.id


def test_mount_records_leave_out_default_raw_settings_so_older_engines_can_read_them(tmp_path: Path) -> None:
    import json

    from mcrawsome.mount.manager import MountedSequence, save_record
    from mcrawsome.recipe.raw import RawCalibration

    record = MountedSequence(
        id="0" * 24, source=tmp_path / "a.mcraw", path=tmp_path / "media", stamp=(1, 2, 3, 4),
        raw_settings=RawCalibration(rotation=90, mount_shading="baked"),
    )  # fmt: skip
    save_record(tmp_path, record)
    saved = json.loads((tmp_path / "sequence.json").read_text())
    assert saved["raw_settings"] == {"rotation": 90, "mount_shading": "baked"}
    assert "frame_range" not in saved
    assert MountedSequence.model_validate(saved) == record.model_copy(update={"in_use": None})


def test_developer_service_starts_in_its_own_session_with_this_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import manager as module
    from mcrawsome.mount.manager import MountedSequence

    monkeypatch.setattr("mcrawsome.mount.runtime._manifest", lambda package: None)
    started: list[tuple[list[str], dict[str, str] | None]] = []

    def launch(command: list[str], **kwargs: object) -> _SlowProcess:
        started.append((command, kwargs.get("env")))  # type: ignore[arg-type]
        assert kwargs["start_new_session"] is True
        return _SlowProcess()

    clock = iter([0.0, 100.0])
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr("mcrawsome.mount.manager.time.monotonic", lambda: next(clock, 100.0))
    monkeypatch.setattr(module, "is_mounted", lambda path: False)
    folder = tmp_path / "f"
    folder.mkdir()
    mount = MountedSequence(
        id="f" * 24, source=tmp_path / "a.mcraw", path=folder / "media", stamp=(1, 2, 3, 4)
    )
    with pytest.raises(McError):
        MountManager()._launch(mount, folder)
    [(command, env)] = started
    assert command == [sys.executable, "-m", "mcrawsome.mount.service", str(folder)]
    assert env is None


def test_the_appimages_service_starts_through_the_image_so_it_outlives_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import manager as module
    from mcrawsome.mount.manager import MountedSequence

    image = str(tmp_path / "mcrawsome.AppImage")
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", image)
    monkeypatch.setenv("MCRAWSOME_LAUNCHER", image)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_mcstu/usr/lib")
    monkeypatch.setenv("MCRAWSOME_ORIGINAL_LD_LIBRARY_PATH", "/opt/host/lib")
    monkeypatch.setattr("mcrawsome.mount.runtime._manifest", lambda package: None)
    started: list[tuple[list[str], dict[str, str]]] = []

    def launch(command: list[str], **kwargs: Any) -> _SlowProcess:
        started.append((command, kwargs["env"]))
        return _SlowProcess()

    clock = iter([0.0, 100.0])
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr("mcrawsome.mount.manager.time.monotonic", lambda: next(clock, 100.0))
    monkeypatch.setattr(module, "is_mounted", lambda path: False)
    folder = tmp_path / "f"
    folder.mkdir()
    mount = MountedSequence(
        id="f" * 24, source=tmp_path / "a.mcraw", path=folder / "media", stamp=(1, 2, 3, 4)
    )
    with pytest.raises(McError):
        MountManager()._launch(mount, folder)
    [(command, environment)] = started
    assert command == [image, "mount-service", str(folder)]
    assert environment["LD_LIBRARY_PATH"] == "/opt/host/lib"  # the image sets up its own again


def test_the_hidden_mount_service_command_serves_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from mcrawsome.cli.main import cli
    from mcrawsome.mount import service

    served: list[Path] = []
    monkeypatch.setattr(service, "serve", served.append)
    result = CliRunner().invoke(cli, ["mount-service", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert served == [tmp_path]
    assert "mount-service" not in CliRunner().invoke(cli, ["--help"]).output


def test_a_service_started_through_an_appimage_counts_as_running(tmp_path: Path) -> None:
    import json
    import sys

    from mcrawsome.mount.manager import process_start, service_running

    folder = tmp_path / "0123456789abcdef01234567"
    folder.mkdir()
    # As AppRun runs it: python -m mcrawsome.cli.main mount-service FOLDER
    sleeper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time,sys;from pathlib import Path;Path(sys.argv[-1], 'awake').touch();time.sleep(30)",
            "mount-service",
            str(folder),
        ]
    )
    try:
        import time

        deadline = time.monotonic() + 5
        while not (folder / "awake").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (folder / "awake").exists()
        (folder / "ready.json").write_text(
            json.dumps({"pid": sleeper.pid, "start": process_start(sleeper.pid)})
        )
        assert service_running(folder)
        (folder / "ready.json").write_text(json.dumps({"pid": sleeper.pid, "start": "another"}))
        assert not service_running(folder)  # a reused process ID is not the service
    finally:
        sleeper.kill()
        sleeper.wait()


BUSY = "fusermount3: failed to unmount /media/one: Device or resource busy"


@pytest.mark.parametrize(
    ("answers", "attempts", "error"),
    [
        ([BUSY, BUSY, ""], 3, None),  # a desktop service probing the new mount held it for a moment
        ([BUSY] * 1000, None, "Media is in use"),  # still held when the wait ends: an editor has it open
        (["fusermount3: entry for /media/one not found in /etc/mtab"], 1, "Could not unmount: fusermount3"),
    ],
)
def test_only_a_busy_unmount_is_tried_again_and_only_for_a_moment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[str],
    attempts: int | None,
    error: str | None,
) -> None:
    from mcrawsome.mount import manager as module

    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        detail = answers[len(calls)]
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 1 if detail else 0, "", detail)

    monkeypatch.setattr(module, "helper", lambda: "fusermount3")
    monkeypatch.setattr(module, "UNMOUNT_WAIT_S", 0.3)
    monkeypatch.setattr(subprocess, "run", run)
    manager = MountManager()
    if error is None:
        manager._unmount(tmp_path / "one")
    else:
        with pytest.raises(McError, match=error):
            manager._unmount(tmp_path / "one")
    assert calls[0] == ["fusermount3", "-u", str(tmp_path / "one")]
    if attempts is not None:
        assert len(calls) == attempts
    else:
        assert 2 < len(calls) < 1000, "a held mount was not tried again, or was tried without end"


@pytest.mark.parametrize("serving", [True, False])
def test_a_mount_whose_service_has_gone_is_detached_even_while_files_are_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serving: bool
) -> None:
    """A dead service leaves a mount point whose files are unreadable already; a normal unmount refuses it
    while an editor still holds them, so Unmount and Remount could not clear it. A live one keeps the
    normal unmount, so held files still protect it."""
    from mcrawsome.mount import manager as module
    from mcrawsome.mount.manager import MountedSequence, save_record

    manager = MountManager()
    identity = "abcdef0123456789abcdef01"
    folder = manager.root / identity
    folder.mkdir(parents=True)
    source = tmp_path / "a.mcraw"
    source.write_bytes(b"clip")
    from mcrawsome.mount.sequence import source_stamp

    record = MountedSequence(
        id=identity, source=source, path=folder / "media", stamp=source_stamp(source), active=True, sent=True
    )
    save_record(folder, record)
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(module, "is_mounted", lambda path: True)
    monkeypatch.setattr(module, "service_running", lambda folder: serving)
    monkeypatch.setattr(module, "helper", lambda: "fusermount3")
    monkeypatch.setattr(subprocess, "run", run)
    manager.unmount(identity)
    monkeypatch.setattr(manager, "_start", lambda mount: mount)
    manager.remount(identity)
    lazy = ["-z"] if not serving else []
    assert calls == [["fusermount3", "-u", *lazy, str(folder / "media")]] * 2


def test_a_log_mount_with_too_few_bits_is_refused_before_a_service_starts(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import manager as module
    from mcrawsome.recipe.raw import RawCalibration

    manager = MountManager()
    monkeypatch.setattr(module, "availability", lambda: (True, "available"))
    started: list[object] = []
    monkeypatch.setattr(manager, "_start", started.append)
    settings = RawCalibration(mount_transfer="log", mount_reduce_bits=8)
    with pytest.raises(McError, match="at least 6") as caught:
        manager.mount(tiny_raw, raw_settings=settings)
    assert caught.value.code == "recipe-invalid"
    assert started == []
    manager.mount(tiny_raw, raw_settings=settings.model_copy(update={"mount_reduce_bits": 2}))
    assert len(started) == 1


def test_a_picture_normalised_mount_is_measured_before_its_service_starts(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import exposure
    from mcrawsome.mount import manager as module
    from mcrawsome.recipe.raw import RawCalibration

    manager = MountManager()
    monkeypatch.setattr(module, "availability", lambda: (True, "available"))
    measured: list[str] = []
    real = exposure.measure

    def measure(*args: object, **kwargs: object) -> list[float]:
        measured.append("measured")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(exposure, "measure", measure)

    def start(mount: object) -> object:
        measured.append("started")
        return mount

    monkeypatch.setattr(manager, "_start", start)
    manager.mount(tiny_raw, raw_settings=RawCalibration(mount_normalise_exposure="picture"))
    manager.mount(tiny_raw, raw_settings=RawCalibration(mount_normalise_exposure="picture"))
    assert measured == ["measured", "started", "started"]  # the second mount reads the kept gains


def test_a_remount_measures_again_when_the_kept_gains_are_gone(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.mount import exposure
    from mcrawsome.mount import manager as module
    from mcrawsome.paths import cache_dir
    from mcrawsome.recipe.raw import RawCalibration

    manager = MountManager()
    monkeypatch.setattr(module, "availability", lambda: (True, "available"))
    started: list[object] = []

    def start(mount: object) -> object:
        started.append(mount)
        return mount

    monkeypatch.setattr(manager, "_start", start)
    mount = manager.mount(tiny_raw, raw_settings=RawCalibration(mount_normalise_exposure="picture"))
    module.save_record(manager.root / mount.id, mount)
    for kept in (cache_dir() / "exposure").iterdir():
        kept.unlink()
    measured: list[int] = []
    real = exposure.measure

    def measure(*args: object, **kwargs: object) -> list[float]:
        measured.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(exposure, "measure", measure)
    manager.remount(mount.id)
    assert measured == [1]
    assert len(started) == 2


def test_level_and_camera_source_policies_make_distinct_retained_mounts(tiny_raw: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    settings = RawCalibration(black_level=42, white_level=3000, mount_camera_model="Panasonic Varicam RAW")
    mounts = []
    try:
        fixed = manager.mount(tiny_raw, raw_settings=settings)
        mounts.append(fixed)
        name = next(fixed.path.glob("*.dng"))
        old_bytes = name.read_bytes()
        for patch in (
            {"level_policy": "recorded"},
            {"level_policy": "static"},
            {"camera_model_source": "recorded"},
        ):
            current = manager.mount(tiny_raw, raw_settings=settings.model_copy(update=patch))
            mounts.append(current)
        assert len({mount.id for mount in mounts}) == 4
        assert name.read_bytes() == old_bytes
        assert MountManager().get(mounts[1].id).raw_settings.level_policy == "recorded"
        assert manager.mount(tiny_raw, raw_settings=settings).id == fixed.id
    finally:
        for mount in mounts:
            manager.unmount(mount.id)


@pytest.mark.parametrize("state", ["empty", "nonempty", "symlink", "active", "starting", "service"])
def test_cleanup_removes_only_empty_inactive_mountpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    from mcrawsome.mount import manager as module
    from mcrawsome.mount.manager import MountedSequence, save_record

    manager = MountManager()
    identity = "0123456789abcdef01234567"
    folder = manager.root / identity
    point = folder / "media"
    folder.mkdir(parents=True)
    if state == "symlink":
        target = tmp_path / "other"
        target.mkdir()
        point.symlink_to(target, target_is_directory=True)
    else:
        point.mkdir()
    if state == "nonempty":
        (point / "keep.txt").write_text("keep")
    mount = MountedSequence(id=identity, source=tmp_path / "gone.mcraw", path=point, stamp=(1, 2, 3, 4))
    save_record(folder, mount)
    monkeypatch.setattr(module, "is_mounted", lambda path: state == "active")
    monkeypatch.setattr(module, "starting", lambda path: state == "starting")
    monkeypatch.setattr(module, "service_running", lambda path: state == "service")
    manager.get(identity)
    assert point.exists() == (state != "empty")
    assert (folder / "sequence.json").is_file()
    if state == "nonempty":
        assert (point / "keep.txt").read_text() == "keep"


def test_panasonic_exposure_fix_does_not_reuse_the_old_mount_generation(tiny_raw: Path) -> None:
    import hashlib
    import json

    from mcrawsome.mount.sequence import source_stamp
    from mcrawsome.recipe.raw import RawCalibration

    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    settings = RawCalibration(mount_camera_model="Panasonic Varicam RAW")
    mount = manager.mount(tiny_raw, raw_settings=settings)
    try:
        old_key = [2, str(tiny_raw.resolve()), source_stamp(tiny_raw), str(mount.mount_root)]
        old_key.append({"raw_settings": settings.model_dump(mode="json", exclude_defaults=True)})
        old_id = hashlib.sha256(json.dumps(old_key).encode()).hexdigest()[:24]
        assert mount.id != old_id
    finally:
        manager.unmount(mount.id)
