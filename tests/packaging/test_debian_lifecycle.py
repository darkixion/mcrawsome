"""Package replacement must stop safely before any installed files are replaced."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest


@pytest.fixture
def lifecycle() -> ModuleType:
    path = Path(__file__).parents[2] / "packaging/deb/package_lifecycle.py"
    spec = importlib.util.spec_from_file_location("debian_lifecycle", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_process_identification_excludes_other_python_commands(lifecycle: ModuleType) -> None:
    assert lifecycle.command(
        ["/usr/bin/python3", "-m", "mcrawsome.cli.main", "server", "--port", "8642"]
    ) == (
        "server",
        ["--port", "8642"],
    )
    assert lifecycle.command(["/usr/bin/python3", "-m", "mcrawsome.mount.service", "/tmp/sequence"]) == (
        "mount-service",
        ["/tmp/sequence"],
    )
    assert lifecycle.command(["/usr/bin/python3", "-m", "unrelated", "server"]) is None
    assert lifecycle.command(["/usr/bin/systemd-run", "mcrawsome", "server"]) is None


def test_busy_mount_aborts_before_saving_or_signalling(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = {"role": "server", "pid": 123, "uid": 1000, "env": {}, "args": []}
    monkeypatch.setattr(lifecycle, "processes", lambda: [engine])
    request = Mock(return_value={"pid": 123, "active_mounts": 1})
    monkeypatch.setattr(lifecycle, "request", request)
    save = Mock()
    signal = Mock()
    monkeypatch.setattr(lifecycle, "save", save)
    monkeypatch.setattr(lifecycle.os, "pidfd_open", signal, raising=False)
    with pytest.raises(RuntimeError, match="Could not unmount"):
        lifecycle.stop(remember=True)
    assert request.call_args_list[1].args == (engine, "/mounts/unmount-all", True)
    save.assert_not_called()
    signal.assert_not_called()


def test_engine_restart_program_uses_isolated_process_launcher(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.paths import state_dir

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def spawn(command: list[str], **options: Any) -> None:
        calls.append((command, options))

    monkeypatch.setattr("mcrawsome.processes.spawn", spawn)
    monkeypatch.setattr("sys.argv", ["restart", "--port", "1234"])
    exec(compile(lifecycle.SPAWN, "package-engine-restart", "exec"), {})
    command, options = calls.pop()
    assert command == ["/usr/bin/mcrawsome", "server", "--port", "1234"]
    assert options["role"] == "engine"
    assert options["stdin"] == subprocess.DEVNULL
    assert options["start_new_session"] is True
    assert Path(options["stdout"].name) == state_dir() / "server.log"
    assert options["stderr"] is options["stdout"]
    assert options["stdout"].closed
