"""The host menu adapter keeps local paths literal and exposes only the two requested actions."""

import json
import runpy
import sys
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


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[list[str]]]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("os.access", lambda path, mode: path == "/usr/bin/mcrawsome")
    config = tmp_path / "mcrawsome/file-manager-actions.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"version": 3, "enabled": True}))
    commands: list[list[str]] = []

    def launch(command: list[str], flags: Any) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(communicate_utf8_async=lambda *args: None)

    repository = SimpleNamespace(
        Nautilus=SimpleNamespace(MenuProvider=type("MenuProvider", (), {}), MenuItem=Item, Menu=Menu),
        GObject=SimpleNamespace(GObject=type("GObject", (), {})),
        Gio=SimpleNamespace(
            Application=SimpleNamespace(get_default=lambda: None),
            Subprocess=SimpleNamespace(new=launch),
            SubprocessFlags=SimpleNamespace(STDOUT_PIPE=1, STDERR_PIPE=2),
        ),
    )
    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(require_version=lambda *args: None))
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    made = runpy.run_path(str(packaging_dir() / "file-managers/nautilus/mcrawsome_menu.py"))
    return made["MCRAWsomeMenu"](), commands


def info(uri: str, *, directory: bool = False) -> SimpleNamespace:
    return SimpleNamespace(get_uri=lambda: uri, is_directory=lambda: directory)


def test_context_labels_and_multifile_argv(provider: tuple[Any, list[list[str]]], tmp_path: Path) -> None:
    extension, commands = provider
    files = [tmp_path / '--browser "quote" $() %.mcraw', tmp_path / "second.MCRAW"]
    [root] = extension.get_file_items([info(path.as_uri()) for path in files])
    assert root.values["label"] == "MCRAWsome"
    assert [item.values["label"] for item in root.menu.items] == [
        "Send to Resolve (Media Bin)",
        "Send to Resolve (Timeline)",
    ]
    for mode, item in zip(("bin", "timeline"), root.menu.items, strict=True):
        item.activate()
        assert commands[-1][1:] == ["send-to-resolve", "--mode", mode, "--", *map(str, files)]
    assert extension.get_background_items(None) == []


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
