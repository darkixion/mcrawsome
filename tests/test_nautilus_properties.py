"""Properties are metadata-only, asynchronous, and limited to one local MCRAW recording."""

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    class Store(list[Any]):
        def append(self, item: Any) -> None:
            super().append(item)

        def remove_all(self) -> None:
            self.clear()

    processes = []
    timers = []

    class Process:
        def __init__(self, argv: list[str], flags: int) -> None:
            self.argv = argv
            self.success = True
            self.output = json.dumps(
                {"frames": 240, "Camera": {"Device": "Test phone"}, "Recording": {"Length": "10 s"}}
            )
            self.forced = False
            processes.append(self)

        def communicate_utf8_async(self, input: Any, cancellable: Any, callback: Any, data: Any) -> None:
            self.callback = callback

        def communicate_utf8_finish(self, result: Any) -> tuple[bool, str, str]:
            return True, self.output, "Cannot read recording" if not self.success else ""

        def get_successful(self) -> bool:
            return self.success

        def force_exit(self) -> None:
            self.forced = True

    def timer(seconds: int, callback: Any) -> int:
        timers.append(callback)
        return 1

    repository = SimpleNamespace(
        Nautilus=SimpleNamespace(
            PropertiesModelProvider=type("Provider", (), {}),
            PropertiesItem=lambda **kw: SimpleNamespace(**kw),
            PropertiesModel=lambda **kw: SimpleNamespace(**kw),
        ),
        GObject=SimpleNamespace(GObject=type("Object", (), {})),
        Gio=SimpleNamespace(
            ListStore=SimpleNamespace(new=lambda kind: Store()),
            Subprocess=SimpleNamespace(new=Process),
            SubprocessFlags=SimpleNamespace(STDOUT_PIPE=1, STDERR_PIPE=2),
        ),
        GLib=SimpleNamespace(timeout_add_seconds=timer, source_remove=lambda identifier: None),
    )
    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(require_version=lambda *args: None))
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    module = runpy.run_path(
        str(Path(__file__).parents[1] / "packaging/file-managers/nautilus/mcrawsome_properties.py")
    )
    return module["MCRAWsomeProperties"](), processes, timers


def info(uri: str, directory: bool = False) -> SimpleNamespace:
    return SimpleNamespace(get_uri=lambda: uri, is_directory=lambda: directory)


def test_properties_populate_after_return_without_shell_interpretation(provider: Any) -> None:
    extension, processes, _timers = provider
    [model] = extension.get_models([info("file:///clips/--quote%20%24%28%29.mcraw")])
    assert model.model[0].value == "Reading metadata…"
    process = processes[0]
    assert process.argv == ["/usr/bin/mcrawsome", "info", "--json", "--", "/clips/--quote $().mcraw"]
    process.callback(process, None, None)
    assert [(row.name, row.value) for row in model.model] == [
        ("Frames", "240"),
        ("Camera: Device", "Test phone"),
        ("Recording: Length", "10 s"),
    ]


@pytest.mark.parametrize(
    "files",
    [
        [],
        [info("file:///a.mcraw"), info("file:///b.mcraw")],
        [info("smb://host/a.mcraw")],
        [info("file:///a.mp4")],
        [info("file:///a.mcraw", True)],
    ],
)
def test_properties_ignore_other_selections(provider: Any, files: list[Any]) -> None:
    extension, processes, _timers = provider
    assert extension.get_models(files) == []
    assert not processes


def test_timeout_ends_reader_and_late_completion_cannot_replace_error(provider: Any) -> None:
    extension, processes, _timers = provider
    [model] = extension.get_models([info("file:///a.mcraw")])
    process = processes[0]
    _timers[0]()
    assert process.forced
    assert "timed out" in model.model[0].value
    process.callback(process, None, None)
    assert "timed out" in model.model[0].value


def test_invalid_recording_shows_error_in_properties(provider: Any) -> None:
    extension, processes, _timers = provider
    [model] = extension.get_models([info("file:///a.mcraw")])
    process = processes[0]
    process.success = False
    process.callback(process, None, None)
    assert model.model[0].value == "Cannot read recording"
