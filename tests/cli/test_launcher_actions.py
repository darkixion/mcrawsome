"""The launcher's actions: Library and Settings open the window there; Unmount All asks the engine to
unmount everything the normal way and says, or notifies, what it did."""

import configparser
import shlex
from typing import Any

import pytest
from click.testing import CliRunner, Result

from mcrawsome import gui, notifications
from mcrawsome.cli.main import cli
from mcrawsome.packaging import packaging_dir


def test_each_launcher_action_runs_a_real_command() -> None:
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str  # type: ignore[assignment,method-assign]
    entry.read(packaging_dir() / "linux/mcrawsome.desktop")
    actions = entry["Desktop Entry"]["Actions"].strip(";").split(";")
    assert actions == ["library", "settings", "unmount-all"]
    for action in actions:
        argv = shlex.split(entry[f"Desktop Action {action}"]["Exec"])
        assert argv[0] == "mcrawsome"
        result = CliRunner().invoke(cli, [*argv[1:], "--help"])
        assert result.exit_code == 0, result.output


def unmount_all(
    monkeypatch: pytest.MonkeyPatch, *, running: bool, results: list[dict[str, Any]], notify: bool = False
) -> tuple[Result, list[tuple[str, str]], list[tuple[str, str, float | None]]]:
    asked: list[tuple[str, str, float | None]] = []
    shown: list[tuple[str, str]] = []

    def request(
        port: int, path: str, method: str = "GET", body: object = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        asked.append((path, method, timeout))
        return {"results": results}

    def show(summary: str, body: str) -> bool:
        shown.append((summary, body))
        return True

    monkeypatch.setattr(gui, "answers", lambda port: running)
    monkeypatch.setattr(gui, "request", request)
    monkeypatch.setattr(notifications, "notify", show)
    result = CliRunner().invoke(cli, ["unmount-all", *(["--notify"] if notify else [])])
    return result, shown, asked


def test_nothing_to_unmount_without_an_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    result, shown, asked = unmount_all(monkeypatch, running=False, results=[], notify=True)
    assert result.exit_code == 0
    assert "Nothing to unmount. No recordings are mounted." in result.output
    assert shown == [("Nothing to unmount", "No recordings are mounted.")]
    assert asked == []


def test_every_mount_unmounted_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [
        {"id": "a", "source": "/media/a.mcraw", "ok": True, "message": "Unmounted"},
        {"id": "b", "source": "/media/b.mcraw", "ok": True, "message": "Unmounted"},
    ]
    result, shown, asked = unmount_all(monkeypatch, running=True, results=results)
    assert result.exit_code == 0
    assert "Unmounted 2 recordings." in result.output
    assert asked == [("/mounts/unmount-all", "POST", 60)]
    assert shown == []


def test_a_recording_with_several_mounts_is_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [
        {"id": "full", "source": "/media/a.mcraw", "ok": True, "message": "Unmounted"},
        {"id": "proxy", "source": "/media/a.mcraw", "ok": True, "message": "Unmounted"},
    ]
    result, _, _ = unmount_all(monkeypatch, running=True, results=results)
    assert result.exit_code == 0
    assert "Unmounted 1 recording. DaVinci Resolve can no longer read it." in result.output


def test_media_still_in_use_is_named_and_fails_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [
        {"id": "a", "source": "/media/a.mcraw", "ok": True, "message": "Unmounted"},
        {"id": "b", "source": "/media/b clip.mcraw", "ok": False, "message": "Could not unmount: busy"},
    ]
    result, shown, _ = unmount_all(monkeypatch, running=True, results=results, notify=True)
    assert result.exit_code == 1
    assert "1 of 2 recordings still mounted. b clip.mcraw: Could not unmount: busy" in result.output
    assert shown == [("1 of 2 recordings still mounted", "b clip.mcraw: Could not unmount: busy")]
