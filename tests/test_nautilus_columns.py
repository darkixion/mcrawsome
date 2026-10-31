"""Files' columns read a folder's recordings in one child process and keep Files responsive; its emblems
follow MCRAWsome's mount records, without asking the engine."""

import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome.cli.main import cli
from mcrawsome.library.scan import recording_columns

COMPLETE, IN_PROGRESS = "complete", "in-progress"


class File:
    def __init__(self, path: Path, directory: bool = False) -> None:
        self.uri = path.as_uri()
        self.directory = directory
        self.emblems: list[str] = []
        self.attributes: dict[str, str] = {}
        self.invalidated = 0

    def get_uri(self) -> str:
        return self.uri

    def is_directory(self) -> bool:
        return self.directory

    def add_emblem(self, name: str) -> None:
        self.emblems.append(name)

    def add_string_attribute(self, name: str, value: str) -> None:
        self.attributes[name] = value

    def invalidate_extension_info(self) -> None:
        self.invalidated += 1


@pytest.fixture
def columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    processes: list[Any] = []
    timers: list[tuple[str, Any]] = []
    completed: list[tuple[Any, Any, Any, str]] = []

    class Process:
        def __init__(self, argv: list[str], flags: int) -> None:
            self.argv = argv
            self.forced = False
            self.answer: dict[str, Any] = {}
            processes.append(self)

        def communicate_utf8_async(self, input: Any, cancellable: Any, callback: Any, data: Any) -> None:
            self.callback = callback

        def communicate_utf8_finish(self, result: Any) -> tuple[bool, str, str]:
            return True, json.dumps(self.answer), ""

        def get_successful(self) -> bool:
            return True

        def force_exit(self) -> None:
            self.forced = True

    def timer(name: str, callback: Any) -> int:
        timers.append((name, callback))
        return len(timers)

    nautilus = SimpleNamespace(
        ColumnProvider=type("ColumnProvider", (), {}),
        InfoProvider=type("InfoProvider", (), {}),
        Column=lambda **options: SimpleNamespace(**options),
        OperationResult=SimpleNamespace(COMPLETE=COMPLETE, IN_PROGRESS=IN_PROGRESS),
        info_provider_update_complete_invoke=lambda *call: completed.append(call),
    )
    repository = SimpleNamespace(
        Nautilus=nautilus,
        GObject=SimpleNamespace(GObject=type("Object", (), {})),
        Gio=SimpleNamespace(
            Subprocess=SimpleNamespace(new=Process),
            SubprocessFlags=SimpleNamespace(STDOUT_PIPE=1, STDERR_SILENCE=4),
        ),
        GLib=SimpleNamespace(
            timeout_add_seconds=lambda seconds, callback: timer(f"{seconds}s", callback),
            timeout_add=lambda milliseconds, callback: timer(f"{milliseconds}ms", callback),
            source_remove=lambda identifier: None,
        ),
    )
    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(require_version=lambda *args: None))
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(tmp_path / "state"))
    module = runpy.run_path(
        str(Path(__file__).parents[1] / "packaging/file-managers/nautilus/mcrawsome_columns.py")
    )
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("")
    module["mount_points"].__globals__["MOUNTINFO"] = mountinfo
    return SimpleNamespace(
        module=module, processes=processes, timers=timers, completed=completed, mountinfo=mountinfo
    )


def run_timers(columns: Any, kind: str) -> None:
    due = [(name, callback) for name, callback in columns.timers if name.endswith(kind)]
    columns.timers[:] = [timer for timer in columns.timers if timer not in due]
    for _name, callback in due:
        callback()


def test_four_columns_are_offered(columns: Any) -> None:
    offered = columns.module["MCRAWsomeColumns"]().get_columns()
    assert [(column.name, column.attribute, column.label) for column in offered] == [
        ("MCRAWsome::duration", "mcrawsome_duration", "Duration"),
        ("MCRAWsome::resolution", "mcrawsome_resolution", "Resolution"),
        ("MCRAWsome::frame_rate", "mcrawsome_frame_rate", "Frame rate"),
        ("MCRAWsome::camera", "mcrawsome_camera", "Camera"),
    ]


def test_a_folder_is_read_in_one_child_process_and_remembered(columns: Any, tmp_path: Path) -> None:
    provider = columns.module["MCRAWsomeColumns"]()
    first, second = tmp_path / "a.mcraw", tmp_path / "b --quote $().MCRAW"
    for path in (first, second):
        path.write_bytes(b"recording")
    files = [File(first), File(second)]
    assert [
        provider.update_file_info_full("p", f"h{index}", "c", file) for index, file in enumerate(files)
    ] == [
        IN_PROGRESS,
        IN_PROGRESS,
    ]
    assert not columns.processes  # gathered first, then read together
    run_timers(columns, "ms")
    [process] = columns.processes
    assert process.argv == ["/usr/bin/mcrawsome", "file-columns", "--", str(first), str(second)]
    process.answer = {str(first): {"duration": "0:00:14", "camera": "Pixel"}, str(second): {}}
    process.callback(process, None, None)
    assert files[0].attributes == {
        "mcrawsome_duration": "0:00:14",
        "mcrawsome_resolution": "",
        "mcrawsome_frame_rate": "",
        "mcrawsome_camera": "Pixel",
    }
    assert set(files[1].attributes.values()) == {""}
    assert columns.completed == [("c", "p", "h0", COMPLETE), ("c", "p", "h1", COMPLETE)]
    again = File(first)
    assert provider.update_file_info_full("p", "h2", "c", again) == COMPLETE
    assert again.attributes["mcrawsome_duration"] == "0:00:14"
    assert len(columns.processes) == 1


@pytest.mark.parametrize("name", ["clip.mp4", "notes.json", "folder.mcraw"])
def test_other_files_are_left_alone(columns: Any, tmp_path: Path, name: str) -> None:
    provider = columns.module["MCRAWsomeColumns"]()
    file = File(tmp_path / name, directory=name == "folder.mcraw")
    assert provider.update_file_info_full("p", "h", "c", file) == COMPLETE
    assert not columns.timers[1:]
    assert not columns.processes
    assert not file.attributes


def test_a_reader_that_hangs_is_stopped_and_its_late_answer_ignored(columns: Any, tmp_path: Path) -> None:
    provider = columns.module["MCRAWsomeColumns"]()
    path = tmp_path / "a.mcraw"
    path.write_bytes(b"recording")
    file = File(path)
    provider.update_file_info_full("p", "h", "c", file)
    run_timers(columns, "ms")
    [process] = columns.processes
    run_timers(columns, "60s")
    assert process.forced
    assert columns.completed == [("c", "p", "h", COMPLETE)]
    process.answer = {str(path): {"duration": "0:00:01"}}
    process.callback(process, None, None)
    assert columns.completed == [("c", "p", "h", COMPLETE)]
    assert file.attributes["mcrawsome_duration"] == ""


def mount(columns: Any, tmp_path: Path, source: Path, in_use: bool) -> None:
    point = tmp_path / "Mounts/a clip-1234"
    record = tmp_path / "state/mounts/1234"
    record.mkdir(parents=True)
    (record / "sequence.json").write_text(json.dumps({"source": str(source), "path": str(point)}))
    start = columns.module["started"](os.getpid())
    (record / "usage.json").write_text(json.dumps({"pid": os.getpid(), "start": start, "in_use": in_use}))
    escaped = str(point).replace(" ", "\\040")
    columns.mountinfo.write_text(f"36 35 0:44 / {escaped} rw - fuse.mcrawsome mcrawsome rw\n")


@pytest.mark.parametrize(("in_use", "emblem"), [(False, "mcrawsome-mounted"), (True, "mcrawsome-in-use")])
def test_mounted_recordings_carry_an_emblem(columns: Any, tmp_path: Path, in_use: bool, emblem: str) -> None:
    source = tmp_path / "a clip.mcraw"
    source.write_bytes(b"recording")
    mount(columns, tmp_path, source, in_use)
    file = File(source)
    columns.module["MCRAWsomeColumns"]().update_file_info_full("p", "h", "c", file)
    assert file.emblems == [emblem]


def test_a_changed_mount_refreshes_only_its_recording(columns: Any, tmp_path: Path) -> None:
    source, other = tmp_path / "a clip.mcraw", tmp_path / "b.mcraw"
    for path in (source, other):
        path.write_bytes(b"recording")
    provider = columns.module["MCRAWsomeColumns"]()
    files = [File(source), File(other)]
    for file in files:
        provider.update_file_info_full("p", "h", "c", file)
    mount(columns, tmp_path, source, in_use=True)
    assert provider.watch() is True
    assert [file.invalidated for file in files] == [1, 0]
    assert provider.watch() is True
    assert [file.invalidated for file in files] == [1, 0]


def test_recording_columns_read_as_the_library_reads_them(tmp_path: Path) -> None:
    clip = Path(__file__).parent / "fixtures/data/clip_10bit_rotated.mcraw"
    if not clip.is_file():
        pytest.skip("approved fixture not fetched")
    assert recording_columns(clip) == {
        "duration": "0:00:00",
        "resolution": "3840x2160",
        "frame_rate": "24 fps",
        "camera": "Google Pixel 10 Pro XL",
    }
    assert recording_columns(tmp_path / "missing.mcraw") == {}


def test_file_columns_answers_a_batch_as_json(tmp_path: Path) -> None:
    from click.testing import CliRunner

    missing = tmp_path / "missing.mcraw"
    result = CliRunner().invoke(cli, ["file-columns", "--", str(missing)])
    assert result.exit_code == 0
    assert json.loads(result.output) == {str(missing): {}}
