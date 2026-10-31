"""Package replacement must stop only identified processes and restart as the original user."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "package_lifecycle", Path(__file__).parents[1] / "packaging/deb/package_lifecycle.py"
)
assert SPEC
assert SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "-m", "other", "server"],
        ["bash", "-c", "mcrawsome server"],
        ["mcrawsome", "info", "clip.mcraw"],
        ["python3", "-m"],
    ],
)
def test_ignores_unrelated_processes(argv: list[str]) -> None:
    assert hook.command(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "-m", "mcrawsome.cli.main", "server", "--port", "18859"],
        ["/usr/bin/mcrawsome", "server", "--port", "18859"],
        ["/usr/bin/python3", "/usr/bin/mcrawsome", "server", "--port", "18859"],
        ["/home/user/mcrawsome-old.AppImage", "server", "--port", "18859"],
    ],
)
def test_recognises_debian_and_older_appimage_engines(argv: list[str]) -> None:
    assert hook.command(argv) == ("server", ["--port", "18859"])


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(hook, "PENDING", tmp_path / "run/engines.json")
    return {
        "pid": 1234,
        "start": "100",
        "uid": 1000,
        "role": "server",
        "args": ["--port", "18859"],
        "env": {"MCRAWSOME_STATE_DIR": str(tmp_path / "state")},
        "cwd": str(tmp_path),
    }


def test_upgrade_stops_by_pid_identity_and_preserves_restart_record(
    engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = {1234: "100"}
    monkeypatch.setattr(hook, "processes", lambda: [engine])
    monkeypatch.setattr(hook, "identity", alive.get)
    monkeypatch.setattr(hook, "request", lambda *args: {"pid": 1234, "active_mounts": 0})
    monkeypatch.setattr(hook.os, "pidfd_open", lambda pid: pid, raising=False)
    monkeypatch.setattr(hook.os, "close", lambda descriptor: None)
    killed = []

    def terminate(descriptor: int, sig: int) -> None:
        killed.append(descriptor)
        alive.pop(descriptor)

    monkeypatch.setattr(hook.signal, "pidfd_send_signal", terminate, raising=False)
    hook.stop(remember=True)
    assert killed == [1234]
    assert hook.pending() == [engine]
    # The next preinst/prerm phase must retain the record when no engine remains.
    monkeypatch.setattr(hook, "processes", list)
    hook.stop(remember=True)
    assert hook.pending() == [engine]
    hook.stop(remember=False)
    assert not hook.PENDING.exists()


def test_busy_mount_prevents_package_removal_without_forcing(
    engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hook, "processes", lambda: [engine])
    calls = []

    def request(process: dict[str, Any], path: str, post: bool = False) -> dict[str, Any]:
        calls.append((path, post))
        return {"pid": 1234, "active_mounts": 1}

    monkeypatch.setattr(hook, "request", request)
    monkeypatch.setattr(
        hook.os, "pidfd_open", lambda pid: pytest.fail("engine must not be killed"), raising=False
    )
    with pytest.raises(RuntimeError, match="Close applications"):
        hook.stop(remember=False)
    assert calls == [("/health", False), ("/mounts/unmount-all", True), ("/health", False)]


def test_reused_pid_is_never_signalled(engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "processes", lambda: [engine])
    monkeypatch.setattr(hook, "identity", lambda pid: "another process")
    monkeypatch.setattr(hook, "request", lambda *args: {"pid": 1234})
    monkeypatch.setattr(hook.os, "pidfd_open", lambda pid: pytest.fail("reused PID"), raising=False)
    hook.stop(remember=False)


def test_restart_drops_privileges_and_restores_original_engine_scope(
    engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    hook.save([engine])
    calls = []
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setattr(hook, "request", lambda *args: {"pid": 5678})
    hook.restart()
    argv, options = calls[0]
    assert argv[:2] == ["/usr/sbin/runuser", "-u"]
    assert argv[-2:] == ["--port", "18859"]
    assert options["env"]["MCRAWSOME_STATE_DIR"] == engine["env"]["MCRAWSOME_STATE_DIR"]
    assert "PYTHONPATH" not in options["env"]
    assert not hook.PENDING.exists()


def test_upgrade_recovers_desktop_auth_from_its_own_window_only(
    engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = {1234: "100", 1235: "101", 1236: "102"}
    window = dict(
        engine,
        pid=1235,
        start="101",
        role="gui",
        env={
            "XAUTHORITY": "/run/user/1000/current-auth",
            "XDG_CURRENT_DESKTOP": "GNOME",
            "MCRAWSOME_STATE_DIR": "/must/not/replace/engine/scope",
        },
    )
    stranger = dict(window, pid=1236, start="102", uid=1001, env={"XAUTHORITY": "/wrong-user"})
    monkeypatch.setattr(hook, "processes", lambda: [engine, window, stranger])
    monkeypatch.setattr(hook, "identity", alive.get)
    monkeypatch.setattr(hook, "request", lambda *args: {"pid": 1234, "active_mounts": 0})
    monkeypatch.setattr(hook.os, "pidfd_open", lambda pid: pid, raising=False)
    monkeypatch.setattr(hook.os, "close", lambda descriptor: None)
    monkeypatch.setattr(hook.signal, "pidfd_send_signal", lambda pid, sig: alive.pop(pid), raising=False)
    hook.stop(remember=True)
    [saved] = hook.pending()
    assert saved["env"]["XAUTHORITY"] == "/run/user/1000/current-auth"
    assert saved["env"]["MCRAWSOME_STATE_DIR"] == engine["env"]["MCRAWSOME_STATE_DIR"]


def test_restart_repairs_legacy_missing_auth_using_user_session_manager(
    engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    engine["env"]["DISPLAY"] = ":0"
    user = SimpleNamespace(pw_name="owner", pw_dir="/home/owner")
    calls = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="XAUTHORITY=/run/user/1000/auth\nXDG_CURRENT_DESKTOP=GNOME\nPYTHONPATH=/untrusted\nMCRAWSOME_STATE_DIR=/wrong\n",
        )

    monkeypatch.setattr(hook.subprocess, "run", run)
    env = hook.desktop_environment(engine, user)
    assert calls[0][0][:4] == ["/usr/sbin/runuser", "-u", "owner", "--"]
    assert env["XAUTHORITY"] == "/run/user/1000/auth"
    assert env["XDG_CURRENT_DESKTOP"] == "GNOME"
    assert env["MCRAWSOME_STATE_DIR"] == engine["env"]["MCRAWSOME_STATE_DIR"]
    assert "PYTHONPATH" not in env


def test_restart_preserves_existing_auth_without_querying_manager(
    engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    engine["env"].update(DISPLAY=":0", XAUTHORITY="/current/auth")
    monkeypatch.setattr(
        hook.subprocess, "run", lambda *args, **kwargs: pytest.fail("unnecessary session query")
    )
    assert (
        hook.desktop_environment(engine, SimpleNamespace(pw_name="owner", pw_dir="/home/owner"))["XAUTHORITY"]
        == "/current/auth"
    )


def test_recognises_pinned_mount_launcher() -> None:
    assert hook.command(
        ["/usr/bin/python3", "-I", "/state/mount-runtimes/abc/mcrawsome-mount-service", "/state/mounts/one"]
    ) == ("mount-service", ["/state/mounts/one"])


def test_upgrade_preserves_busy_pinned_service_and_only_stops_engine(
    engine: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    folder = tmp_path / "mount"
    folder.mkdir()
    (folder / "sequence.json").write_text(json.dumps({"path": "/media/one", "id": "one"}))
    service = dict(engine, pid=1235, start="101", role="mount-service", args=[str(folder)])
    alive = {1234: "100", 1235: "101"}
    monkeypatch.setattr(hook, "processes", lambda: [engine, service])
    monkeypatch.setattr(hook, "identity", alive.get)
    monkeypatch.setattr(hook, "mounted", lambda path: True)
    preserved = {"id": "one", "pid": 1235, "start": "101", "uid": 1000}
    monkeypatch.setattr(hook, "preserved_service", lambda process: preserved)
    calls = []

    def request(process: dict[str, Any], path: str, post: bool = False) -> dict[str, Any]:
        calls.append((path, post))
        return (
            {"pid": 1234, "active_mounts": 1}
            if path == "/health"
            else {"mounts": [{"id": "one", "active": True}]}
        )

    monkeypatch.setattr(hook, "request", request)
    monkeypatch.setattr(hook.os, "pidfd_open", lambda pid: pid, raising=False)
    monkeypatch.setattr(hook.os, "close", lambda fd: None)
    monkeypatch.setattr(hook.signal, "pidfd_send_signal", lambda pid, sig: alive.pop(pid), raising=False)
    monkeypatch.setattr(
        hook.subprocess, "run", lambda *args, **kw: pytest.fail("must not unmount or restart services")
    )
    hook.stop(remember=True, preserve_mounts=True)
    assert alive == {1235: "101"}
    assert calls == [("/health", False), ("/mounts", False)]
    assert hook.pending()[0]["preserved_mounts"] == [preserved]


def test_busy_legacy_mount_blocks_first_upgrade_without_signalling(
    engine: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    folder = tmp_path / "mount"
    folder.mkdir()
    (folder / "sequence.json").write_text(json.dumps({"path": "/media/one"}))
    service = dict(engine, role="mount-service", args=[str(folder)])
    monkeypatch.setattr(hook, "processes", lambda: [service])
    monkeypatch.setattr(hook, "identity", lambda pid: "100")
    monkeypatch.setattr(hook, "mounted", lambda path: True)
    monkeypatch.setattr(hook, "preserved_service", lambda process: None)
    commands = []
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kwargs: commands.append(argv))
    monkeypatch.setattr(
        hook.os, "pidfd_open", lambda pid: pytest.fail("must not signal busy legacy mount"), raising=False
    )
    with pytest.raises(RuntimeError, match="predates independent mount services"):
        hook.stop(remember=True, preserve_mounts=True)
    assert commands[0][-3:] == ["/usr/bin/fusermount3", "-u", "/media/one"]


@pytest.mark.parametrize(
    ("service", "mounts", "adopted"),
    [
        ("101", [{"id": "one", "active": True, "serving": True}], True),
        # Exited (ejected, say): its media is unmounted, so there is nothing left to adopt.
        ("different process", [], True),
        ("101", [{"id": "one", "active": True, "serving": False}], False),
        ("101", [], False),
    ],
)
def test_restart_needs_adoption_only_of_preserved_services_still_running(
    engine: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    mounts: list[dict[str, Any]],
    adopted: bool,
) -> None:
    engine["preserved_mounts"] = [{"id": "one", "pid": 1235, "start": "101", "uid": 1000}]
    hook.save([engine])
    monkeypatch.setattr(hook.subprocess, "run", lambda *args, **kw: None)
    monkeypatch.setattr(
        hook, "request", lambda engine, path: {"pid": 5678} if path == "/health" else {"mounts": mounts}
    )
    monkeypatch.setattr(hook, "identity", lambda pid: service)
    if adopted:
        hook.restart()
        assert not hook.PENDING.exists()
    else:
        with pytest.raises(RuntimeError, match=r"did not adopt.*eject that media"):
            hook.restart()
        assert hook.PENDING.exists()
