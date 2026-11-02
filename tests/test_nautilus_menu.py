"""The host menu adapter keeps local paths literal and exposes the two Send actions, under Mount or Unmount
as the recordings' state says."""

import json
import os
import runpy
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome.packaging import packaging_dir


class Item:
    def __init__(self, **values: Any) -> None:
        self.values = values
        self.callback: Any = None
        self.arguments: tuple[Any, ...] = ()
        self.menu: Any = None

    def connect(self, signal: str, callback: Any, *arguments: Any) -> None:
        self.callback, self.arguments = callback, arguments

    def set_submenu(self, menu: Any) -> None:
        self.menu = menu

    def activate(self) -> None:
        self.callback(self, *self.arguments)


class Menu:
    def __init__(self) -> None:
        self.items: list[Item] = []

    def append_item(self, item: Item) -> None:
        self.items.append(item)


class MenuProvider:
    items_updated = 0

    def emit_items_updated_signal(self) -> None:
        self.items_updated += 1


class Notification:
    def __init__(self, title: str) -> None:
        self.title = title
        self.body = ""

    def set_body(self, body: str) -> None:
        self.body = body


class Process:
    """A command the menu started, which a test finishes with the command's outcome."""

    def __init__(self) -> None:
        self.answer = (True, "", "")
        self.callback: Any = None
        self.data: Any = None

    def communicate_utf8_async(self, stdin: Any, cancellable: Any, callback: Any, data: Any) -> None:
        self.callback, self.data = callback, data

    def communicate_utf8_finish(self, result: Any) -> tuple[bool, str, str]:
        return self.answer[0], self.answer[1], self.answer[2]

    def get_successful(self) -> bool:
        return self.answer[0]

    def finish(self, *, successful: bool = True, output: str = "", error: str = "") -> None:
        self.answer = (successful, output, error)
        self.callback(self, None, self.data)


@pytest.fixture
def shown() -> list[tuple[str, str, str]]:
    """Each notification's identity, title and body."""
    return []


@pytest.fixture
def started() -> list[Process]:
    return []


@pytest.fixture
def provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shown: list[tuple[str, str, str]],
    started: list[Process],
) -> Iterator[tuple[Any, list[list[str]]]]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("os.access", lambda path, mode: path == "/usr/bin/mcrawsome")
    config = tmp_path / "mcrawsome/file-manager-actions.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"version": 3, "enabled": True}))
    commands: list[list[str]] = []

    def launch(command: list[str], flags: Any) -> Process:
        commands.append(command)
        started.append(Process())
        return started[-1]

    def send_notification(identity: str, notification: Notification) -> None:
        shown.append((identity, notification.title, notification.body))

    repository = SimpleNamespace(
        Nautilus=SimpleNamespace(
            MenuProvider=MenuProvider,
            MenuItem=Item,
            Menu=Menu,
            ColumnProvider=type("ColumnProvider", (), {}),
            InfoProvider=type("InfoProvider", (), {}),
        ),
        GObject=SimpleNamespace(GObject=type("GObject", (), {})),
        GLib=SimpleNamespace(),
        Gio=SimpleNamespace(
            Application=SimpleNamespace(
                get_default=lambda: SimpleNamespace(send_notification=send_notification)
            ),
            Notification=SimpleNamespace(new=Notification),
            Subprocess=SimpleNamespace(new=launch),
            SubprocessFlags=SimpleNamespace(STDOUT_PIPE=1, STDERR_PIPE=2),
        ),
    )
    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(require_version=lambda *args: None))
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    # Files puts its extension folder on the module path, so the menu finds the emblems' records beside it.
    monkeypatch.syspath_prepend(packaging_dir() / "file-managers/nautilus")
    monkeypatch.delitem(sys.modules, "mcrawsome_columns", raising=False)
    made = runpy.run_path(str(packaging_dir() / "file-managers/nautilus/mcrawsome_menu.py"))
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("")
    sys.modules["mcrawsome_columns"].MOUNTINFO = mountinfo  # type: ignore[attr-defined]
    yield made["MCRAWsomeMenu"](), commands
    del sys.modules["mcrawsome_columns"]


def info(uri: str, *, directory: bool = False) -> SimpleNamespace:
    return SimpleNamespace(get_uri=lambda: uri, is_directory=lambda: directory)


def test_context_labels_and_multifile_argv(provider: tuple[Any, list[list[str]]], tmp_path: Path) -> None:
    extension, commands = provider
    files = [tmp_path / '--browser "quote" $() %.mcraw', tmp_path / "second.MCRAW"]
    [root] = extension.get_file_items([info(path.as_uri()) for path in files])
    assert root.values["label"] == "MCRAWsome"
    assert [item.values["label"] for item in root.menu.items] == [
        "Mount",
        "Send to Resolve (Media Bin)",
        "Send to Resolve (Timeline)",
    ]
    root.menu.items[0].activate()
    assert commands[-1][1:] == ["mount", "--", *map(str, files)]
    for mode, item in zip(("bin", "timeline"), root.menu.items[1:], strict=True):
        item.activate()
        assert commands[-1][1:] == ["send-to-resolve", "--mode", mode, "--", *map(str, files)]
    assert extension.get_background_items(None) == []


def mounted(tmp_path: Path, source: Path, *, in_use: bool = False) -> None:
    """A recording's mount record and mount point, as the engine leaves them."""
    point = tmp_path / f"Mounts/{source.stem} 1234"
    record = tmp_path / f"state/mounts/{source.stem}"
    record.mkdir(parents=True)
    (record / "sequence.json").write_text(json.dumps({"source": str(source), "path": str(point)}))
    start = sys.modules["mcrawsome_columns"].started(os.getpid())
    (record / "usage.json").write_text(json.dumps({"pid": os.getpid(), "start": start, "in_use": in_use}))
    escaped = str(point).replace(" ", "\\040")
    with (tmp_path / "mountinfo").open("a") as lines:
        lines.write(f"36 35 0:44 / {escaped} rw - fuse.mcrawsome mcrawsome rw\n")


def test_unmount_replaces_mount_while_a_selected_recording_is_mounted(
    provider: tuple[Any, list[list[str]]], tmp_path: Path
) -> None:
    extension, commands = provider
    first, second = tmp_path / "first.mcraw", tmp_path / "second.mcraw"
    mounted(tmp_path, second)
    [root] = extension.get_file_items([info(first.as_uri()), info(second.as_uri())])
    toggle = root.menu.items[0]
    assert toggle.values == {"name": "MCRAWsome::Resolve::unmount", "label": "Unmount", "sensitive": True}
    toggle.activate()
    assert commands == [["/usr/bin/mcrawsome", "unmount", "--", str(first), str(second)]]
    [root] = extension.get_file_items([info(first.as_uri())])
    assert root.menu.items[0].values == {
        "name": "MCRAWsome::Resolve::mount",
        "label": "Mount",
        "sensitive": True,
    }


def test_a_link_to_a_mounted_recording_offers_unmount(
    provider: tuple[Any, list[list[str]]], tmp_path: Path
) -> None:
    extension, commands = provider
    recording, link = tmp_path / "recording.mcraw", tmp_path / "link.mcraw"
    recording.touch()
    link.symlink_to(recording)
    mounted(tmp_path, recording)
    [root] = extension.get_file_items([info(link.as_uri())])
    assert root.menu.items[0].values["label"] == "Unmount"
    root.menu.items[0].activate()
    assert commands[-1][1:] == ["unmount", "--", str(link)]  # the path Files gave, as the send does


def test_a_mount_record_without_a_mount_offers_mount(
    provider: tuple[Any, list[list[str]]], tmp_path: Path
) -> None:
    extension, _ = provider
    recording = tmp_path / "recording.mcraw"
    mounted(tmp_path, recording)
    (tmp_path / "mountinfo").write_text("")
    [root] = extension.get_file_items([info(recording.as_uri())])
    assert root.menu.items[0].values["label"] == "Mount"


def test_open_files_in_an_editor_disable_unmount_for_the_selection(
    provider: tuple[Any, list[list[str]]], tmp_path: Path
) -> None:
    extension, _ = provider
    free, held = tmp_path / "free.mcraw", tmp_path / "held.mcraw"
    mounted(tmp_path, free)
    mounted(tmp_path, held, in_use=True)
    [root] = extension.get_file_items([info(free.as_uri())])
    assert root.menu.items[0].values["sensitive"] is True
    [root] = extension.get_file_items([info(free.as_uri()), info(held.as_uri())])
    assert root.menu.items[0].values["label"] == "Unmount"
    assert root.menu.items[0].values["sensitive"] is False


def test_mount_says_it_has_started_then_what_it_did(
    provider: tuple[Any, list[list[str]]],
    shown: list[tuple[str, str, str]],
    started: list[Process],
    tmp_path: Path,
) -> None:
    extension, _ = provider
    [root] = extension.get_file_items([info((tmp_path / "clip.mcraw").as_uri())])
    root.menu.items[0].activate()
    assert shown == [
        ("mcrawsome-mounts", "MCRAWsome", "Mounting recordings. The MCRAWsome window does not need to open.")
    ]
    assert extension.items_updated == 0
    started[-1].finish(output="Preparing clip.mcraw\nMounted 1 recording in /home/me/Mounts\n")
    assert shown[-1] == ("mcrawsome-mounts", "MCRAWsome: mounted", "Mounted 1 recording in /home/me/Mounts")
    assert extension.items_updated == 1  # Files asks again, so the same selection now offers Unmount


@pytest.mark.parametrize(
    ("answer", "title", "body"),
    [
        (
            {"output": "Unmounted 1 recording. DaVinci Resolve can no longer read it.\n"},
            "MCRAWsome: unmounted",
            "Unmounted 1 recording. DaVinci Resolve can no longer read it.",
        ),
        (
            {"successful": False, "error": "Error: 1 of 1 recording still mounted. clip.mcraw: In use\n"},
            "MCRAWsome: could not unmount",
            "1 of 1 recording still mounted. clip.mcraw: In use",
        ),
    ],
)
def test_unmount_says_what_it_did_or_what_stayed_mounted(
    provider: tuple[Any, list[list[str]]],
    shown: list[tuple[str, str, str]],
    started: list[Process],
    tmp_path: Path,
    answer: dict[str, Any],
    title: str,
    body: str,
) -> None:
    extension, _ = provider
    recording = tmp_path / "clip.mcraw"
    mounted(tmp_path, recording)
    [root] = extension.get_file_items([info(recording.as_uri())])
    root.menu.items[0].activate()
    assert shown == []
    started[-1].finish(**answer)
    assert shown == [("mcrawsome-mounts", title, body)]
    assert extension.items_updated == 1


def test_a_command_that_cannot_start_is_reported(
    provider: tuple[Any, list[list[str]]],
    shown: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, commands = provider
    [root] = extension.get_file_items([info("file:///clip.mcraw")])
    monkeypatch.setattr("os.access", lambda path, mode: False)
    root.menu.items[0].activate()
    root.menu.items[1].activate()
    assert not commands
    install = "Install the MCRAWsome Debian package first."
    assert shown == [
        ("mcrawsome-mounts", "MCRAWsome: could not mount", install),
        ("mcrawsome-resolve-send", "MCRAWsome: could not send to Resolve", install),
    ]


def test_a_send_reports_under_its_own_notification(
    provider: tuple[Any, list[list[str]]],
    shown: list[tuple[str, str, str]],
    started: list[Process],
) -> None:
    extension, _ = provider
    [root] = extension.get_file_items([info("file:///clip.mcraw")])
    root.menu.items[1].activate()
    assert shown == [
        (
            "mcrawsome-resolve-send",
            "MCRAWsome",
            "Preparing recordings for Resolve. The MCRAWsome window does not need to open.",
        )
    ]
    started[-1].finish(output="Sent 1 recording to Resolve. Complete the import in Resolve.\n")
    assert shown[-1] == (
        "mcrawsome-resolve-send",
        "MCRAWsome: sent to Resolve",
        "Sent 1 recording to Resolve. Complete the import in Resolve.",
    )
    root.menu.items[1].activate()
    started[-1].finish(successful=False, error="Error: Resolve handoff could not be opened\n")
    assert shown[-1] == (
        "mcrawsome-resolve-send",
        "MCRAWsome: could not send to Resolve",
        "Resolve handoff could not be opened",
    )
    assert extension.items_updated == 2


@pytest.mark.parametrize("uri", ["smb://server/clip.mcraw", "file://other/clip.mcraw", "file:///clip.mp4"])
def test_remote_and_mixed_selections_have_no_mcrawsome_menu(
    provider: tuple[Any, list[list[str]]], uri: str
) -> None:
    extension, commands = provider
    assert not extension.get_file_items([info("file:///clip.mcraw"), info(uri)])
    assert not extension.get_file_items([info("file:///folder.mcraw", directory=True)])
    assert not commands


@pytest.mark.parametrize(
    "saved",
    [
        {"version": 1, "appimage": "/tmp/old.AppImage"},
        {"version": 2, "executable": "/tmp/other-program"},
        {"version": 2, "executable": ["/usr/bin/mcrawsome"]},
    ],
)
def test_invalid_or_old_configuration_never_launches(
    provider: tuple[Any, list[list[str]]],
    tmp_path: Path,
    saved: dict[str, Any],
) -> None:
    extension, commands = provider
    (tmp_path / "mcrawsome/file-manager-actions.json").write_text(json.dumps(saved))
    assert extension.get_file_items([info("file:///clip.mcraw")]) == []
    assert commands == []


def test_default_and_persistent_disable(provider: tuple[Any, list[list[str]]], tmp_path: Path) -> None:
    extension, commands = provider
    config = tmp_path / "mcrawsome/file-manager-actions.json"
    config.unlink()
    [root] = extension.get_file_items([info("file:///clip.mcraw")])
    config.write_text(json.dumps({"version": 3, "enabled": False}))
    assert extension.get_file_items([info("file:///clip.mcraw")]) == []
    root.menu.items[0].activate()
    assert commands == []
    config.write_text(json.dumps({"version": 3, "enabled": True}))
    [root] = extension.get_file_items([info("file:///clip.mcraw")])
    root.menu.items[0].activate()
    assert len(commands) == 1
