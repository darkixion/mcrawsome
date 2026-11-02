"""The launcher's actions: Library and Settings open the window there; Unmount All asks the engine to
unmount everything the normal way and says, or notifies, what it did."""

import configparser
import shlex
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from mcrawsome import gui, notifications
from mcrawsome.cli.main import cli
from mcrawsome.mount import manager
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
    monkeypatch: pytest.MonkeyPatch,
    *,
    running: bool,
    results: list[dict[str, Any]],
    notify: bool = False,
    mounted: bool = False,
    started: list[int] | None = None,
) -> tuple[Result, list[tuple[str, str]], list[tuple[str, str, float | None]]]:
    """The command against an engine answering `results`. `mounted`: media recorded as mounted, whether or
    not an engine runs. An engine started without a `started` list to record it fails the command."""
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

    def start(port: int, roots: list[Path]) -> None:
        assert started is not None, "an engine was started"
        started.append(port)

    monkeypatch.setattr(gui, "answers", lambda port: running)
    monkeypatch.setattr(gui, "request", request)
    monkeypatch.setattr(gui, "start_server", start)
    monkeypatch.setattr(manager, "mounted_sources", lambda: {Path("/media/a.mcraw")} if mounted else set())
    monkeypatch.setattr(notifications, "notify", show)
    result = CliRunner().invoke(cli, ["unmount-all", *(["--notify"] if notify else [])])
    return result, shown, asked


def test_nothing_to_unmount_without_an_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    result, shown, asked = unmount_all(monkeypatch, running=False, results=[], notify=True)
    assert result.exit_code == 0
    assert "Nothing to unmount. No recordings are mounted." in result.output
    assert shown == [("Nothing to unmount", "No recordings are mounted.")]
    assert asked == []


def test_media_that_outlived_the_engine_starts_one_to_unmount_it(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [{"id": "a", "source": "/media/a.mcraw", "ok": True, "message": "Unmounted"}]
    started: list[int] = []
    result, shown, asked = unmount_all(
        monkeypatch, running=False, results=results, notify=True, mounted=True, started=started
    )
    assert result.exit_code == 0, result.output
    assert len(started) == 1
    assert asked == [("/mounts/unmount-all", "POST", 60)]
    assert shown == [("Unmounted 1 recording", "DaVinci Resolve can no longer read it.")]


def test_a_running_engine_is_asked_without_starting_another(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [{"id": "a", "source": "/media/a.mcraw", "ok": True, "message": "Unmounted"}]
    result, _, asked = unmount_all(monkeypatch, running=True, results=results, mounted=True)
    assert result.exit_code == 0, result.output
    assert asked == [("/mounts/unmount-all", "POST", 60)]


def test_an_engine_that_cannot_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(port: int, roots: list[Path]) -> None:
        raise RuntimeError("the server stopped")

    monkeypatch.setattr(gui, "answers", lambda port: False)
    monkeypatch.setattr(gui, "start_server", refuse)
    monkeypatch.setattr(manager, "mounted_sources", lambda: {Path("/media/a.mcraw")})
    result = CliRunner().invoke(cli, ["unmount-all"])
    assert result.exit_code == 1
    assert "Could not reach MCRAWsome's engine: the server stopped" in result.output


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
