"""The desktop window shares the local engine and leaves queued work running."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from mcrawsome import gui
from mcrawsome.cli.main import cli
from mcrawsome.webkit import attach_shutdown


def test_without_pywebview_the_page_opens_in_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    said: list[str] = []
    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.setattr("webbrowser.open", opened.append)
    assert gui.show("http://127.0.0.1:8642/", said.append) is False
    assert opened == ["http://127.0.0.1:8642/"]
    assert "pywebview" in said[0]


def test_nothing_answering_is_not_a_server() -> None:
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    assert gui.answers(port, timeout=0.2) is False


def test_the_page_is_the_interface() -> None:
    assert gui.page_url(9000, None) == "http://127.0.0.1:9000/"


def test_two_windows_use_the_engine_already_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(gui, "answers", lambda port: True)

    def no_server(port: int, roots: list[Path]) -> None:
        raise AssertionError("started a second server")

    monkeypatch.setattr(gui, "start_server", no_server)
    for _ in range(2):
        gui.launch(9123, [], lambda message: None, open_window=lambda url, api: opened.append(url))
    assert opened == ["http://127.0.0.1:9123/"] * 2


@pytest.mark.parametrize(
    ("owned", "keep", "busy", "stops"),
    [
        (True, False, False, True),
        (True, False, True, False),
        (True, True, False, False),
        (False, False, False, False),
    ],
)
def test_closing_preserves_work_and_other_peoples_engines(
    monkeypatch: pytest.MonkeyPatch, owned: bool, keep: bool, busy: bool, stops: bool
) -> None:
    stopped: list[int] = []
    monkeypatch.setattr(gui, "answers", lambda port: not owned)
    monkeypatch.setattr(gui, "start_server", lambda port, roots: SimpleNamespace(pid=123, poll=lambda: None))

    def request(port: int, path: str, method: str = "GET") -> dict[str, Any]:
        if path == "/health":
            return {"pid": 123, "active_mounts": int(busy)}
        if path == "/settings":
            return {"keep_engine": keep}
        if path == "/jobs":
            return {"jobs": [{"state": {"status": "queued"}}] if busy else []}
        assert path == "/engine/stop"
        assert method == "POST"
        stopped.append(port)
        return {}

    monkeypatch.setattr(gui, "request", request)
    gui.launch(9123, [], lambda message: None, open_window=lambda url, api: None)
    assert stopped == ([9123] if stops else [])


def test_browser_option_returns_with_the_engine_running(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(gui, "answers", lambda port: False)
    monkeypatch.setattr(gui, "start_server", lambda port, roots: SimpleNamespace(pid=123, poll=lambda: None))
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = CliRunner().invoke(cli, ["gui", "--port", "9123", "--browser"])
    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:9123/"]


def test_restart_reuses_a_live_engine_and_starts_a_stopped_one(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []
    live = True
    monkeypatch.setattr(gui, "answers", lambda port: live)

    def start_server(port: int, roots: list[Path]) -> SimpleNamespace:
        started.append(port)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(gui, "start_server", start_server)
    bridge = gui.Bridge(9123, [])
    bridge.restart_engine()
    assert started == []
    live = False
    bridge.restart_engine()
    assert started == [9123]


@pytest.mark.parametrize("missing", ["gi", "WebKitGTK"])
def test_window_failure_names_the_missing_part(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    opened: list[str] = []
    said: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    def unavailable(url: str, bridge: gui.Bridge | None) -> None:
        if missing == "gi":
            raise ModuleNotFoundError("No module named gi", name="gi")
        raise gui.WindowUnavailableError("Namespace WebKit2 not available")

    assert not gui.show("http://127.0.0.1:9123/", said.append, open_window=unavailable)
    assert missing in said[0]
    assert opened == ["http://127.0.0.1:9123/"]


def test_the_native_window_uses_the_brand_and_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []
    started: list[str] = []
    handlers: list[Callable[..., Any]] = []

    class Event:
        def __iadd__(self, handler: Callable[..., Any]) -> "Event":
            handlers.append(handler)
            return self

    native = SimpleNamespace(events=SimpleNamespace(before_show=Event(), loaded=Event()))

    def create(title: str, url: str, **options: Any) -> object:
        created.append({"title": title, "url": url, **options})
        return native

    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(
            create_window=create, start=lambda gui, user_agent: started.append(f"{gui}:{user_agent}")
        ),
    )
    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(require_version=lambda name, version: None))
    bridge = gui.Bridge(9123, [])
    assert gui.show("http://127.0.0.1:9123/", lambda message: None, bridge)
    assert created == [
        {
            "title": gui.TITLE,
            "url": "http://127.0.0.1:9123/",
            "width": 1440,
            "height": 900,
            "min_size": (1024, 700),
            "js_api": bridge,
        }
    ]
    assert bridge._window is native
    assert len(handlers) == 3
    assert handlers[1] == bridge.prepare_preview  # the preview surface exists before the page is shown
    assert handlers[-1] == attach_shutdown
    assert started == ["gtk:mcrawsome-pywebview"]


def test_gui_opens_the_requested_clip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    opened: list[str] = []
    clip = tmp_path / "synthetic.mcraw"
    clip.touch()
    monkeypatch.setattr(gui, "answers", lambda port: True)
    monkeypatch.setattr(gui, "request", lambda *args: {"clips": [{"id": "abc", "path": str(clip)}]})
    gui.launch(
        9123, [tmp_path], lambda message: None, clip=clip, open_window=lambda url, api: opened.append(url)
    )
    assert opened == ["http://127.0.0.1:9123/?open=1#/library"]


def _through(client: TestClient) -> Callable[..., dict[str, Any]]:
    """`gui.request` answered by a real app."""

    def request(
        port: int, path: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with closing(
            TestClient(client.app, base_url=str(client.base_url), client=("127.0.0.1", 1234))
        ) as local:
            response = local.request(method, f"/api/v1{path}", json=body)
        assert response.status_code == 200, response.text
        loaded: dict[str, Any] = response.json()
        return loaded

    return request


def _ready_clip(write_mcraw: Callable[..., Path], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_mcraw(path, [i * 41_666_667 for i in range(3)])
    when = time.time() - 3600
    os.utime(path, (when, when))
    return path


SAVED = {
    "show_preview_stats": False,
    "mount_folder": None,
    "preview_gpu": False,
    "quad_bayer_remosaic": True,
    "preview_cache_mib": 64,
    "workers": 3,
    "keep_engine": True,
    "remount_on_start": False,
    "resolve_full_resolution": False,
    "resolve_separate_audio": False,
    "resolve_link_audio": True,
    "resolve_keywords": False,
    "timecode": "zero",
    "timecode_zone": "computer",
    "resolve_route": "menu",
}


def test_gui_opens_temporary_references_and_keeps_all_library_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient, make_mcraw: Callable[..., Path]
) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    saved = {"folders": [str(previous)]} | SAVED
    assert client.put("/api/v1/settings", json=saved).status_code == 200
    clip = _ready_clip(make_mcraw, tmp_path / "elsewhere" / "synthetic.mcraw")
    said: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(gui, "answers", lambda port: True)
    monkeypatch.setattr(gui, "request", _through(client))
    gui.launch(9123, [], said.append, clip=clip, open_window=lambda url, api: opened.append(url))
    settings = client.get("/api/v1/settings").json()
    assert settings == saved | {
        "folders": [str(previous)],
        "problem": None,
        "resolve_route": "auto",
        "mount_folder_default": str(tmp_path / "mounts"),
    }
    assert not said
    assert not client.get("/api/v1/library").json()["clips"]
    (listed,) = client.get("/api/v1/opened-clips").json()["clips"]
    assert listed["path"] == str(clip)
    assert opened == ["http://127.0.0.1:9123/?open=1#/library"]


def test_gui_finds_the_clip_in_a_new_engines_library_root(
    monkeypatch: pytest.MonkeyPatch,
    library: Path,
    client: TestClient,
    make_mcraw: Callable[..., Path],
    tmp_path: Path,
) -> None:
    # The engine started here has the clip's folder as its --library root but not as a saved folder.
    assert client.put("/api/v1/settings", json={"folders": []} | SAVED).status_code == 200
    clip = _ready_clip(make_mcraw, library / "synthetic.mcraw")
    started: list[list[Path]] = []

    def start(port: int, roots: list[Path]) -> SimpleNamespace:
        started.append(roots)
        return SimpleNamespace(pid=123, poll=lambda: None)

    said: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(gui, "answers", lambda port: bool(started))
    monkeypatch.setattr(gui, "start_server", start)
    monkeypatch.setattr(gui, "request", _through(client))
    gui.launch(9123, [library], said.append, clip=clip, open_window=lambda url, api: opened.append(url))
    assert started == [[library]]
    assert client.get("/api/v1/settings").json() == {
        "folders": [],
    } | SAVED | {"problem": None, "resolve_route": "auto", "mount_folder_default": str(tmp_path / "mounts")}
    assert not any("Added" in message for message in said)
    (listed,) = client.get("/api/v1/opened-clips").json()["clips"]
    assert listed["path"] == str(clip)
    assert opened == ["http://127.0.0.1:9123/?open=1#/library"]


def test_gui_leaves_unreadable_settings_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: TestClient
) -> None:
    clip = tmp_path / "elsewhere" / "synthetic.mcraw"
    clip.parent.mkdir()
    clip.touch()
    puts: list[str] = []

    def request(
        port: int, path: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        puts.extend([path] if method == "PUT" else [])
        if path == "/settings":
            return {"folders": [], "keep_engine": True, "problem": "interface.toml: bad"}
        return {"clips": []}

    monkeypatch.setattr(gui, "answers", lambda port: True)
    monkeypatch.setattr(gui, "request", request)
    gui.launch(9123, [], lambda message: None, clip=clip, open_window=lambda url, api: None)
    assert puts == []


def test_folders_open_with_xdg_open_and_pages_in_the_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[list[str]] = []
    opened: list[str] = []
    monkeypatch.setattr("subprocess.run", lambda command, **options: started.append(command))
    monkeypatch.setattr("webbrowser.open", opened.append)
    gui.Bridge(9123, []).open_folder(str(tmp_path))
    gui.open_page("http://127.0.0.1:9123/")
    assert started == [["xdg-open", str(tmp_path)]]
    assert opened == ["http://127.0.0.1:9123/"]


def test_a_notification_goes_to_the_desktops_notification_service(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **options: Any) -> Any:
        started.append((command, options))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", run)
    assert gui.Bridge(9123, []).notify("3 clips sent to Resolve (Timeline)", 'Complete "the" import ✓')
    [(command, options)] = started
    assert command == [
        "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
        "--object-path", "/org/freedesktop/Notifications", "--method", "org.freedesktop.Notifications.Notify",
        '"MCRAWsome"', "uint32 0", '"mcrawsome"', '"3 clips sent to Resolve (Timeline)"',
        '"Complete \\"the\\" import ✓"', "@as []", '@a{sv} {"desktop-entry": <"mcrawsome">}', "int32 -1",
    ]  # fmt: skip
    assert options["check"]
    assert options["timeout"] == 5


def test_a_desktop_without_a_notification_service_is_told_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **options: Any) -> Any:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("subprocess.run", run)
    assert gui.Bridge(9123, []).notify("Sent", "Complete the import in Resolve.") is False


def test_revealing_a_file_uses_the_desktop_file_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip ' #雪.mcraw"
    source.touch()
    started: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **options: Any) -> Any:
        started.append((command, options))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", run)
    gui.Bridge(9123, []).reveal_file(str(source))
    [(command, options)] = started
    assert command == [
        "gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
        "--object-path", "/org/freedesktop/FileManager1", "--method",
        "org.freedesktop.FileManager1.ShowItems", json.dumps([source.as_uri()]), "",
    ]  # fmt: skip
    assert options["check"]
    assert options["timeout"] == 5
    with pytest.raises(ValueError, match="file"):
        gui.Bridge(9123, []).reveal_file(str(tmp_path))
    with pytest.raises(ValueError, match="file"):
        gui.Bridge(9123, []).reveal_file(str(tmp_path / "missing.mcraw"))
    assert len(started) == 1


def test_the_engine_starts_with_this_python(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(gui, "answers", lambda port: True)

    def start(command: list[str], **options: Any) -> Any:
        started.append(command)
        return SimpleNamespace(poll=lambda: None, wait=lambda: 0)

    monkeypatch.setattr("subprocess.Popen", start)
    gui.start_server(9123, [])
    assert started == [[sys.executable, "-m", "mcrawsome.cli.main", "server", "--port", "9123"]]


def test_runtime_error_after_window_creation_does_not_open_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    def broken(url: str, bridge: gui.Bridge | None) -> None:
        raise RuntimeError("after close")

    with pytest.raises(RuntimeError, match="after close"):
        gui.show("http://localhost/", lambda message: None, open_window=broken)
    assert opened == []


def test_local_requests_send_the_configured_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import io
    import json

    from mcrawsome.server.config import config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text('token = "test-secret"\n')
    seen: list[str | None] = []

    class Response(io.BytesIO):
        status = 200

    def respond(query: Any, **options: Any) -> Response:
        seen.append(query.get_header("Authorization"))
        return Response(json.dumps({"version": "1", "pid": 123}).encode())

    monkeypatch.setattr("urllib.request.urlopen", respond)
    assert gui.answers(9123)
    gui.request(9123, "/library")
    assert seen == ["Bearer test-secret"] * 2


def test_opening_a_copying_clip_waits_without_adding_its_subfolder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clip = tmp_path / "child" / "synthetic.mcraw"
    calls = 0
    puts: list[str] = []
    said: list[str] = []

    def request(port: int, path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        nonlocal calls
        if method == "PUT":
            puts.append(path)
        if path == "/settings":
            return {"folders": [str(tmp_path)]}
        if path == "/health":
            return {}
        calls += 1
        return {"clips": [{"id": "abc" if calls > 1 else None, "path": str(clip), "status": "copying"}]}

    monkeypatch.setattr(gui, "answers", lambda port: True)
    monkeypatch.setattr(gui, "request", request)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    result = gui.wait_for_clip(9123, clip, said.append)
    assert not puts
    assert any("Waiting" in message for message in said)
    assert result["id"] == "abc"


def test_library_connection_failure_is_plain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def failed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise urllib.error.URLError("connection lost")

    monkeypatch.setattr(gui, "answers", lambda port: True)
    monkeypatch.setattr(gui, "request", failed)
    with pytest.raises(RuntimeError, match=r"cannot open recordings.*connection lost"):
        gui.launch(9123, [], lambda message: None, clip=tmp_path / "synthetic.mcraw")


def test_engine_ownership_requires_the_health_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    process = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(gui, "request", lambda port, path: {"version": "1", "pid": 456})
    assert not gui.owns_engine(9123, process)  # type: ignore[arg-type]
    monkeypatch.setattr(gui, "request", lambda port, path: {"version": "1", "pid": 123})
    assert gui.owns_engine(9123, process)  # type: ignore[arg-type]


def test_server_log_rotates_before_start(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_bytes(b"old output")
    gui.rotate_log(log, max_bytes=5)
    assert not log.exists()
    assert log.with_suffix(".log.1").read_bytes() == b"old output"


def test_local_requests_use_the_server_token_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCRAWSOME_SERVER_TOKEN", "environment-secret")
    captured: list[str | None] = []

    def respond(query: Any, **options: Any) -> Any:
        captured.append(query.get_header("Authorization"))
        raise OSError("no server")

    monkeypatch.setattr("urllib.request.urlopen", respond)
    assert not gui.answers(9123)
    assert captured == ["Bearer environment-secret"]


def test_owns_engine_matches_the_pid_the_real_health_reports(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setattr(gui, "request", _through(client))
    ours = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
    other = SimpleNamespace(pid=os.getpid() + 1, poll=lambda: None)
    assert gui.owns_engine(9123, ours)  # type: ignore[arg-type]
    assert not gui.owns_engine(9123, other)  # type: ignore[arg-type]
    assert not gui.owns_engine(9123, SimpleNamespace(pid=os.getpid(), poll=lambda: 1))  # type: ignore[arg-type]


def test_closing_the_window_stops_an_engine_this_run_started(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    stopped: list[str] = []
    real = _through(client)

    def request(
        port: int, path: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if path == "/engine/stop":
            stopped.append(method)
            return {}
        return real(port, path, method, body)  # health, settings and jobs come from the real app

    started: list[int] = []

    def start(port: int, roots: list[Path]) -> SimpleNamespace:
        started.append(port)
        return SimpleNamespace(
            pid=os.getpid(), poll=lambda: None
        )  # the engine under test runs in this process

    monkeypatch.setattr(gui, "answers", lambda port: bool(started))
    monkeypatch.setattr(gui, "start_server", start)
    monkeypatch.setattr(gui, "request", request)
    gui.launch(9123, [], lambda message: None, open_window=lambda url, api: None)
    assert stopped == ["POST"]


def test_missing_media_elements_leave_audio_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    gst = SimpleNamespace(init=lambda unused: None, ElementFactory=SimpleNamespace(find=lambda name: None))
    gi = SimpleNamespace(require_version=lambda name, version: None)
    modules = {"gi": gi, "gi.repository.Gst": gst}
    monkeypatch.setattr("mcrawsome.gui.importlib.import_module", modules.__getitem__)
    assert gui.Bridge(9000, []).media_available() is False
    gst.ElementFactory.find = lambda name: object()
    assert gui.Bridge(9000, []).media_available() is True


# ---- An engine from another build ----


class _OldEngine:
    """An engine answering with another build: its health, queue and stop request."""

    def __init__(self, jobs: list[str], build: str | None = "0ld") -> None:
        self.jobs = jobs
        self.build = build
        self.stopped = False

    def answers(self, port: int) -> bool:
        return not self.stopped

    def request(self, port: int, path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        if path == "/health":
            health: dict[str, Any] = {
                "version": "0.0.1",
                "pid": 77,
                "active_mounts": sum(status not in ("done", "failed", "cancelled") for status in self.jobs),
            }
            return health | ({"build": self.build} if self.build is not None else {})
        if path == "/settings":
            return {"keep_engine": True}
        assert (path, method) == ("/engine/stop", "POST")
        assert all(status in ("done", "failed", "cancelled") for status in self.jobs)
        self.stopped = True
        return {"stopping": True}


@pytest.mark.parametrize("build", ["0ld", None])  # an engine from before /health had a build
def test_an_idle_engine_from_another_build_is_restarted(
    monkeypatch: pytest.MonkeyPatch, build: str | None
) -> None:
    engine = _OldEngine(["done", "failed"], build)
    started: list[int] = []
    said: list[str] = []
    monkeypatch.setattr(gui, "answers", engine.answers)
    monkeypatch.setattr(gui, "request", engine.request)

    def start(port: int, roots: list[Path]) -> SimpleNamespace:
        started.append(port)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(gui, "start_server", start)
    gui.launch(9123, [], said.append, open_window=lambda url, api: None)
    assert engine.stopped
    assert started == [9123]
    assert any("MCRAWsome 0.0.1" in line and "restarting it" in line for line in said), said


def test_a_busy_engine_from_another_build_is_kept_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _OldEngine(["running", "queued", "done"])
    said: list[str] = []
    bridges: list[gui.Bridge | None] = []
    monkeypatch.setattr(gui, "answers", engine.answers)
    monkeypatch.setattr(gui, "request", engine.request)
    monkeypatch.setattr(gui, "start_server", lambda port, roots: pytest.fail("restarted a busy engine"))
    gui.launch(9123, [], said.append, open_window=lambda url, api: bridges.append(api))
    assert not engine.stopped
    message = next(line for line in said if "MCRAWsome 0.0.1" in line)
    assert "2 mounts active" in message
    assert "when its mounts are released" in message
    assert bridges[0] is not None
    assert bridges[0].stale == "MCRAWsome 0.0.1 (build 0ld)"


def test_the_window_offers_the_restart_once_the_queue_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _OldEngine(["running"])
    asked: list[str] = []
    loaded: list[str] = []
    started: list[int] = []
    monkeypatch.setattr(gui, "answers", engine.answers)
    monkeypatch.setattr(gui, "request", engine.request)

    def start(port: int, roots: list[Path]) -> SimpleNamespace:
        started.append(port)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(gui, "start_server", start)
    polls = iter([False, True])

    def wait(seconds: float) -> bool:
        if next(polls):
            engine.jobs = ["done"]
        return False

    def confirm(title: str, text: str) -> bool:
        asked.append(text)
        return True

    window = SimpleNamespace(create_confirmation_dialog=confirm, load_url=loaded.append)
    bridge = gui.Bridge(9123, [])
    bridge.stale = "MCRAWsome 0.0.1 (build 0ld)"
    gui.offer_restart(bridge, window, "http://127.0.0.1:9123/", wait=wait)
    assert len(asked) == 1
    assert "MCRAWsome 0.0.1 (build 0ld)" in asked[0]
    assert engine.stopped
    assert started == [9123]
    assert loaded == ["http://127.0.0.1:9123/"]
    assert bridge.stale is None


def test_the_same_build_is_used_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome import __version__
    from mcrawsome.build import build_id

    engine = _OldEngine(["done"], build_id())
    monkeypatch.setattr(
        engine, "request", lambda port, path, *a: {"version": __version__, "build": build_id()}
    )
    monkeypatch.setattr(gui, "answers", engine.answers)
    monkeypatch.setattr(gui, "request", engine.request)
    monkeypatch.setattr(gui, "start_server", lambda port, roots: pytest.fail("restarted the same build"))
    bridges: list[gui.Bridge | None] = []
    gui.launch(9123, [], lambda message: None, open_window=lambda url, api: bridges.append(api))
    assert bridges[0] is not None
    assert bridges[0].stale is None


def test_the_build_id_follows_the_interface_bundle(tmp_path: Path) -> None:
    from mcrawsome.build import package_hash

    package = tmp_path / "mcrawsome"
    (package / "server" / "web").mkdir(parents=True)
    (package / "gui.py").write_text("print('a')\n")
    (package / "server" / "web" / "index.html").write_text("<script src=a.js>")
    first = package_hash(package)
    (package / "server" / "web" / "index.html").write_text("<script src=b.js>")
    second = package_hash(package)
    assert second != first
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "gui.cpython-312.pyc").write_bytes(b"\0")
    assert package_hash(package) == second  # compiled files differ between machines


def test_mounted_media_prevents_engine_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(port: int, path: str, method: str = "GET") -> dict[str, Any]:
        if path == "/jobs":
            return {"jobs": []}
        assert path == "/health"
        return {"active_mounts": 2}

    monkeypatch.setattr(gui, "request", request)
    assert gui.active_media(8642) == 2


def test_native_preview_is_optional_when_gtk_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gi", None)
    bridge = gui.Bridge(9000, [])
    assert not bridge.preview_available(1)
    assert not bridge.preview_present(1, 0, {}, {})


def test_preparing_the_native_preview_without_gtk_leaves_the_page_picture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "gi", None)
    bridge = gui.Bridge(9000, [])
    bridge.prepare_preview()
    assert not bridge.preview_available(1)


def test_native_bridge_rejects_invalid_descriptors_before_creating_widgets() -> None:
    bridge = gui.Bridge(9000, [])
    assert not bridge.preview_available(-1)
    assert not bridge.preview_present(1, 0, {"name": "../outside"}, {})
    assert not bridge.preview_bounds(1, 0, {})


def test_profile_dialogs_validate_and_bound_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = {
        "version": 1,
        "sensor_width": 128,
        "sensor_height": 64,
        "cfa": "rggb",
        "map_width": 2,
        "map_height": 2,
        "gains": [[1.0] * 4] * 4,
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    calls: list[tuple[object, dict[str, Any]]] = []

    def dialog(kind: object, **options: Any) -> tuple[str]:
        calls.append((kind, options))
        return (str(path),)

    monkeypatch.setitem(
        sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(OPEN="open", SAVE="save"))
    )
    bridge = gui.Bridge(9000, [])
    bridge._window = SimpleNamespace(create_file_dialog=dialog)
    loaded = bridge.load_vignette_profile()
    assert loaded is not None
    assert loaded["gains"] == profile["gains"]
    assert bridge.save_vignette_profile(profile, "../test.mcraw")
    assert calls[-1][0] == "save"
    assert calls[-1][1]["save_filename"] == "test.mcraw.vignette.json"
    assert json.loads(path.read_text())["gains"] == profile["gains"]
    path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="2 MiB"):
        bridge.load_vignette_profile()
    path.write_text("{}")
    with pytest.raises(ValueError, match="validation errors"):
        bridge.load_vignette_profile()
    bridge._window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: None)
    assert bridge.load_vignette_profile() is None
    assert bridge.save_vignette_profile(profile, "test") is False


def test_report_save_is_bounded_json_and_uses_native_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "report.json"
    calls: list[dict[str, Any]] = []

    def dialog(kind: object, **options: Any) -> tuple[str]:
        calls.append(options)
        return (str(target),)

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save")))
    bridge = gui.Bridge(9000, [])
    bridge._window = SimpleNamespace(create_file_dialog=dialog)
    assert bridge.save_report('{"samples": 2}', "../counts.json")
    assert json.loads(target.read_text()) == {"samples": 2}
    assert calls[0]["save_filename"] == "counts.json"
    with pytest.raises(ValueError, match="JSON"):
        bridge.save_report("not json", "bad.json")
    with pytest.raises(ValueError, match="8 MiB"):
        bridge.save_report(" " * (8 * 1024 * 1024 + 1), "huge.json")


def test_lists_save_only_as_csv_ale_or_gpx_through_the_native_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "chosen.gpx"
    calls: list[dict[str, Any]] = []

    def dialog(kind: object, **options: Any) -> tuple[str]:
        calls.append(options)
        return (str(target),)

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save")))
    bridge = gui.Bridge(9000, [])
    bridge._window = SimpleNamespace(create_file_dialog=dialog)
    assert bridge.save_text("<gpx/>", "../clip-locations.gpx")
    assert target.read_text() == "<gpx/>"
    assert calls[0] == {"save_filename": "clip-locations.gpx", "file_types": ("GPS exchange (*.gpx)",)}
    with pytest.raises(ValueError, match="CSV, ALE and GPX"):
        bridge.save_text("x", "script.sh")
    with pytest.raises(ValueError, match="8 MiB"):
        bridge.save_text(" " * (8 * 1024 * 1024 + 1), "huge.csv")


def test_native_file_drop_forwards_full_paths_without_reading_or_opening_them() -> None:
    from mcrawsome.gui_drop import forward_drop

    scripts: list[str] = []
    window = SimpleNamespace(evaluate_js=scripts.append)
    forward_drop(
        window,
        {
            "dataTransfer": {
                "files": [
                    {"name": "a.mcraw", "pywebviewFullPath": '/clips/take "A".mcraw'},
                    {"name": "hidden-path.mcraw"},
                ]
            }
        },
    )
    assert len(scripts) == 1
    assert "mcrawsome-files-dropped" in scripts[0]
    assert json.dumps(['/clips/take "A".mcraw']) in scripts[0]
    assert "hidden-path" not in scripts[0]


def test_launch_recovers_when_the_old_engine_disconnects_during_health_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from http.client import RemoteDisconnected

    live = True
    probes = 0
    started = []
    opened = []
    messages: list[str] = []
    monkeypatch.setattr(gui, "answers", lambda port: live)
    monkeypatch.setattr(gui, "owns_engine", lambda port, process: False)

    def request(port: int, path: str, method: str = "GET") -> dict[str, Any]:
        nonlocal live, probes
        probes += 1
        assert path == "/health"
        if probes == 1:
            return {"version": "0.0.1", "build": "old"}
        live = False
        raise RemoteDisconnected("Remote end closed connection without response")

    def start(port: int, roots: list[Path]) -> SimpleNamespace:
        started.append(port)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(gui, "request", request)
    monkeypatch.setattr(gui, "start_server", start)
    monkeypatch.setattr(gui, "replace_engine", lambda *_: pytest.fail("stopped a dead engine"))
    gui.launch(9123, [], messages.append, open_window=lambda url, bridge: opened.append(url))
    assert started == [9123]
    assert opened == ["http://127.0.0.1:9123/"]
    assert any("previous engine stopped" in message.lower() for message in messages)


def test_a_transient_health_disconnect_is_retried_without_restarting_active_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from http.client import RemoteDisconnected

    attempts = 0
    bridges: list[gui.Bridge | None] = []
    monkeypatch.setattr(gui, "answers", lambda port: True)
    monkeypatch.setattr(gui, "start_server", lambda *_: pytest.fail("started a duplicate engine"))
    monkeypatch.setattr(gui, "replace_engine", lambda *_: pytest.fail("stopped mounted media"))
    monkeypatch.setattr("mcrawsome.gui.time.sleep", lambda _: None)

    def request(port: int, path: str, method: str = "GET") -> dict[str, Any]:
        nonlocal attempts
        assert path == "/health"
        attempts += 1
        if attempts == 2:
            raise RemoteDisconnected("connection dropped")
        return {"version": "0.0.1", "build": "old", "active_mounts": 2}

    monkeypatch.setattr(gui, "request", request)
    gui.launch(9123, [], lambda _: None, open_window=lambda url, bridge: bridges.append(bridge))
    assert attempts == 3
    assert bridges[0] is not None
    assert bridges[0].stale is not None


def test_an_unreadable_health_keeps_a_live_engine_and_opens_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from http.client import RemoteDisconnected

    probes = 0
    messages: list[str] = []
    bridges: list[gui.Bridge | None] = []
    monkeypatch.setattr(gui, "answers", lambda port: True)
    monkeypatch.setattr(gui, "start_server", lambda *_: pytest.fail("started a duplicate engine"))
    monkeypatch.setattr(gui, "replace_engine", lambda *_: pytest.fail("stopped an engine with unknown work"))
    monkeypatch.setattr("mcrawsome.gui.time.sleep", lambda _: None)

    def request(port: int, path: str, method: str = "GET") -> dict[str, Any]:
        nonlocal probes
        probes += 1
        assert path == "/health"
        if probes == 1:
            return {"version": "0.0.1", "build": "old"}
        raise RemoteDisconnected("connection dropped")

    monkeypatch.setattr(gui, "request", request)
    gui.launch(9123, [], messages.append, open_window=lambda url, bridge: bridges.append(bridge))
    assert bridges[0] is not None
    assert bridges[0].stale is not None
    assert any("could not check" in message.lower() for message in messages)


def test_launch_recovers_from_a_real_http_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    probes = 0

    class OldEngine(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal probes
            probes += 1
            assert self.path == "/api/v1/health"
            if probes <= 2:
                payload = json.dumps({"version": "0.0.1", "build": "old"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                self.server.server_close()
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args: Any) -> None:
            pass

    started = []
    opened = []
    with HTTPServer(("127.0.0.1", 0), OldEngine) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        port = server.server_address[1]
        monkeypatch.setattr(gui, "start_server", lambda port, roots: started.append(port))
        monkeypatch.setattr(gui, "owns_engine", lambda *_: False)
        gui.launch(port, [], lambda _: None, open_window=lambda url, bridge: opened.append(url))
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert started == [port]
    assert opened == [f"http://127.0.0.1:{port}/"]


def test_capture_csv_save_streams_atomically_and_preserves_destination_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import io

    target = tmp_path / "capture.csv"
    target.write_bytes(b"previous file")
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save")))
    bridge = gui.Bridge(9123, [])
    bridge._window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: (str(target),))
    urls = []

    class Response(io.BytesIO):
        def __init__(self, data: bytes = b"") -> None:
            super().__init__(data)
            self.headers = {"Content-Type": "text/csv; charset=utf-8"}

    def response(url: str, **kwargs: Any) -> Response:
        urls.append(url)
        return Response(b"frame,iso\r\n1,100\r\n")

    monkeypatch.setattr("mcrawsome.gui.urllib.request.urlopen", response)
    assert bridge.save_capture_csv("abc123", "clip.mcraw")
    assert urls == ["http://127.0.0.1:9123/api/v1/clips/abc123/capture.csv"]
    assert target.read_bytes() == b"frame,iso\r\n1,100\r\n"

    class Broken(Response):
        def read(self, count: int | None = -1) -> bytes:
            raise OSError("disconnected")

    monkeypatch.setattr("mcrawsome.gui.urllib.request.urlopen", lambda *args, **kwargs: Broken())
    with pytest.raises(OSError, match="disconnected"):
        bridge.save_capture_csv("abc123", "clip.mcraw")
    assert target.read_bytes() == b"frame,iso\r\n1,100\r\n"
    assert not list(tmp_path.glob(".mcrawsome-capture-*"))
    with pytest.raises(ValueError, match="identity"):
        bridge.save_capture_csv("../../other", "clip")


def test_session_dialog_uses_a_supported_json_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    chosen: list[dict[str, Any]] = []
    monkeypatch.setitem(
        sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save", OPEN="open"))
    )
    bridge = gui.Bridge(9123, [])
    bridge._window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: chosen.append(kwargs))
    assert bridge.choose_session(True) is None
    assert bridge.choose_session(False) is None
    assert chosen[0]["save_filename"] == "Untitled.mcrawsome-session.json"
    assert chosen[0]["file_types"] == chosen[1]["file_types"] == ("MCRAWsome sessions (*.json)",)


@pytest.mark.parametrize(
    "typed",
    # The dialog offers Untitled.mcrawsome-session.json, the part before the last dot selected to type over.
    ["Test.json", "Test", "Test.mcrawsome-session", "Test.mcrawsome-session.json"],
)
def test_a_session_saved_under_a_typed_name_gets_the_session_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, typed: str
) -> None:
    monkeypatch.setitem(
        sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save", OPEN="open"))
    )
    bridge = gui.Bridge(9123, [])
    bridge._window = SimpleNamespace(
        create_file_dialog=lambda *args, **kwargs: (str(tmp_path / typed),),
        create_confirmation_dialog=lambda *args: pytest.fail("asked although nothing would be replaced"),
    )
    assert bridge.choose_session(True) == str(tmp_path / "Test.mcrawsome-session.json")
    assert bridge.choose_session(False) == str(tmp_path / typed)  # an opened file keeps its name


def test_completing_a_session_name_asks_before_replacing_a_session_the_dialog_did_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(
        sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save", OPEN="open"))
    )
    (tmp_path / "Test.mcrawsome-session.json").write_text("{}")
    asked: list[str] = []
    answers = [False, True]

    def confirm(title: str, message: str) -> bool:
        asked.append(message)
        return answers.pop(0)

    bridge = gui.Bridge(9123, [])
    bridge._window = SimpleNamespace(
        create_file_dialog=lambda *args, **kwargs: (str(tmp_path / "Test.json"),),
        create_confirmation_dialog=confirm,
    )
    assert bridge.choose_session(True) is None
    assert bridge.choose_session(True) == str(tmp_path / "Test.mcrawsome-session.json")
    assert len(asked) == 2
    assert "Test.mcrawsome-session.json" in asked[0]


def test_calibration_target_uses_native_svg_save_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target.svg"
    calls: list[dict[str, Any]] = []

    def dialog(kind: object, **options: Any) -> tuple[str]:
        calls.append(options)
        return (str(target),)

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save")))
    bridge = gui.Bridge(9000, [])
    bridge._window = SimpleNamespace(create_file_dialog=dialog)
    assert bridge.save_calibration_target()
    assert "Move the phone" in target.read_text()
    assert calls[0]["file_types"] == ("SVG target (*.svg)",)
    assert calls[0]["save_filename"] == "mcrawsome-lens-target.svg"


def test_camera_profile_download_refreshes_profiles_and_uses_toml_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcrawsome.camera.profile import CameraProfile, Match, all_profiles, save_profile

    target = tmp_path / "download.toml"
    all_profiles.cache_clear()
    all_profiles()  # The desktop can already have cached the list before the engine saves a new profile.
    profile = CameraProfile(name="download_camera", match=Match(device_model="Test phone", camera_id="0/2"))
    save_profile(profile)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save")))
    bridge = gui.Bridge(9000, [])
    bridge._window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: (str(target),))
    try:
        assert bridge.save_camera_profile("download_camera")
        assert 'camera_id = "0/2"' in target.read_text()
        with pytest.raises(ValueError, match="does not exist"):
            bridge.save_camera_profile("missing")
    finally:
        all_profiles.cache_clear()


# The AppImage: AppRun points the bundle's variables at its own files and keeps the host's originals.
APPDIR = "/tmp/.mount_mcstuXYZ"
INSIDE = {
    "MCRAWSOME_APPIMAGE": "/home/me/mcrawsome.AppImage",
    "MCRAWSOME_LAUNCHER": "/home/me/mcrawsome.AppImage",
    "APPIMAGE": "/home/me/mcrawsome.AppImage",
    "APPDIR": APPDIR,
    "HOME": "/home/me",
    "PATH": f"{APPDIR}/usr/bin:/usr/bin",
    "LD_LIBRARY_PATH": f"{APPDIR}/usr/lib:{APPDIR}/usr/python/lib",
    "LD_PRELOAD": f"{APPDIR}/usr/lib/mcrawsome-webkit-launcher.so",
    "XDG_DATA_DIRS": f"{APPDIR}/usr/share:/usr/share",
    "GI_TYPELIB_PATH": f"{APPDIR}/usr/lib/girepository-1.0",
    "GIO_MODULE_DIR": f"{APPDIR}/usr/lib/gio/modules",
    "GDK_PIXBUF_MODULE_FILE": "/run/user/1000/mcrawsome-pixbuf.abc",
    "GST_PLUGIN_SYSTEM_PATH_1_0": f"{APPDIR}/usr/lib/gstreamer-1.0",
    "GST_PLUGIN_SCANNER": f"{APPDIR}/usr/libexec/gstreamer-1.0/gst-plugin-scanner",
    "WEBKIT_EXEC_PATH": f"{APPDIR}/usr/lib/webkit2gtk-4.1",
    "WEBKIT_INJECTED_BUNDLE_PATH": f"{APPDIR}/usr/lib/webkit2gtk-4.1/injected-bundle",
    "MCRAWSOME_WEBKIT_HELPERS": f"{APPDIR}/usr/lib/webkit2gtk-4.1",
    "MCRAWSOME_FFMPEG": f"{APPDIR}/usr/bin/ffmpeg",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "MCRAWSOME_ORIGINAL_PATH": "/usr/bin",
    "MCRAWSOME_ORIGINAL_LD_LIBRARY_PATH": "/opt/host/lib",
    "MCRAWSOME_ORIGINAL_LD_PRELOAD": "",
    "MCRAWSOME_ORIGINAL_XDG_DATA_DIRS": "/usr/share",
}
HOST = {
    "HOME": "/home/me",
    "PATH": "/usr/bin",
    "LD_LIBRARY_PATH": "/opt/host/lib",
    "XDG_DATA_DIRS": "/usr/share",
}


def _inside(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith("MCRAWSOME_ORIGINAL_"):
            monkeypatch.delenv(name)
    for name, value in INSIDE.items():
        monkeypatch.setenv(name, value)


def test_host_programs_get_the_hosts_environment() -> None:
    from mcrawsome.appimage import host_environment

    host = host_environment(INSIDE)
    assert host == HOST
    assert not any(APPDIR in value for value in host.values())
    extracted = {
        name: value for name, value in INSIDE.items() if name not in ("MCRAWSOME_APPIMAGE", "APPIMAGE")
    }
    extracted["MCRAWSOME_LAUNCHER"] = f"{APPDIR}/AppRun"
    assert host_environment(extracted) == HOST


def test_outside_the_appimage_the_environment_is_unchanged() -> None:
    from mcrawsome.appimage import host_environment

    environment = {"APPIMAGE": "/home/me/Terminal.AppImage", "PATH": "/usr/bin", "LD_PRELOAD": "x.so"}
    assert host_environment(environment) == environment


def test_host_programs_recover_extra_host_gio_modules() -> None:
    from mcrawsome.appimage import host_environment

    environment = host_environment(INSIDE | {"MCRAWSOME_ORIGINAL_GIO_EXTRA_MODULES": "/host/gio/modules"})
    assert environment["GIO_EXTRA_MODULES"] == "/host/gio/modules"


def test_the_appimages_engine_starts_through_the_image_with_the_hosts_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inside(monkeypatch)
    started: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(gui, "answers", lambda port: True)

    def start(command: list[str], **options: Any) -> Any:
        started.append((command, options["env"]))
        return SimpleNamespace(poll=lambda: None, wait=lambda: 0)

    monkeypatch.setattr("subprocess.Popen", start)
    gui.start_server(9123, [])
    [(command, environment)] = started
    assert command == ["/home/me/mcrawsome.AppImage", "server", "--port", "9123"]
    assert environment["PATH"] == "/usr/bin"
    assert "GI_TYPELIB_PATH" not in environment


def test_folders_pages_and_files_open_with_the_hosts_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inside(monkeypatch)
    started: list[tuple[list[str], dict[str, str]]] = []

    def run(command: list[str], **options: Any) -> Any:
        started.append((command, options["env"]))
        return SimpleNamespace(returncode=0, poll=lambda: 0, wait=lambda: 0)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr("subprocess.Popen", run)
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("opened with the bundle's environment"))
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    bridge = gui.Bridge.__new__(gui.Bridge)
    bridge.open_folder(str(tmp_path))
    gui.open_page("http://127.0.0.1:9123/")
    bridge.reveal_file(str(clip))
    assert [command[:2] for command, _ in started] == [
        ["xdg-open", str(tmp_path)],
        ["xdg-open", "http://127.0.0.1:9123/"],
        ["gdbus", "call"],
    ]
    for _, environment in started:
        assert environment["LD_LIBRARY_PATH"] == "/opt/host/lib"
        assert "LD_PRELOAD" not in environment
        assert not any(APPDIR in value for value in environment.values())


def test_the_appimage_offers_a_menu_entry_once_and_keeps_other_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.server.settings import InterfaceSettings, load_settings, save_settings

    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(tmp_path / "mcrawsome.AppImage"))
    save_settings(InterfaceSettings(workers=2))
    asked: list[str] = []
    monkeypatch.setattr("mcrawsome.desktop.desktop_entry", lambda: pytest.fail("installed when declined"))
    monkeypatch.setattr("mcrawsome.desktop.desktop_entry_status", lambda: ("current-image", False))

    def decline(title: str, message: str) -> bool:
        asked.append(message)
        return False

    window = SimpleNamespace(create_confirmation_dialog=decline)
    gui.offer_desktop_entry(window)
    gui.offer_desktop_entry(window)  # declined for this image: not asked again
    assert len(asked) == 1
    assert ".mcraw" in asked[0]
    settings, problem = load_settings()
    assert problem is None
    assert settings.workers == 2
    assert settings.desktop_entry_offer == "current-image:declined"
    save_settings(settings.model_copy(update={"workers": 3, "desktop_entry_offer": None}))
    assert load_settings()[0].desktop_entry_offer == "current-image:declined"  # an engine's save keeps it


def test_another_appimage_is_a_normal_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "Terminal.AppImage"))
    monkeypatch.delenv("MCRAWSOME_APPIMAGE", raising=False)
    window = SimpleNamespace(create_confirmation_dialog=lambda title, message: pytest.fail("offered"))
    gui.offer_desktop_entry(window)
