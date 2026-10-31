import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from mcrawsome import __version__
from mcrawsome.cli.main import cli
from mcrawsome.doctor import Check
from mcrawsome.errors import ErrorCode, McError
from mcrawsome.server import app as app_module
from mcrawsome.server.app import create_app
from mcrawsome.server.config import ServerConfig, load_config

API = "/api/v1"
BASE = "http://127.0.0.1:8642"
ROUTES = {
    "/api/v1/camera-profiles",
    "/api/v1/camera-capture",
    "/api/v1/camera-capture/{name}",
    "/api/v1/camera-capture/analyse",
    "/api/v1/camera-capture/recordings",
    "/api/v1/camera-profiles/analyse",
    "/api/v1/about",
    "/api/v1/support/report",
    "/api/v1/about/licences/{file}",
    "/api/v1/health",
    "/api/v1/offload",
    "/api/v1/offload/cancel",
    "/api/v1/offload/scan",
    "/api/v1/engine/stop",
    "/api/v1/events",
    "/api/v1/colour",
    "/api/v1/camera-profiles/target.svg",
    "/api/v1/camera-profiles/{name}.toml",
    "/api/v1/doctor",
    "/api/v1/mounts",
    "/api/v1/mounts/{mount_id}/remount",
    "/api/v1/mounts/{mount_id}/unmount",
    "/api/v1/resolve/mounts/send",
    "/api/v1/resolve/timeline/rates",
    "/api/v1/clips/{clip_id}/audio",
    "/api/v1/clips/{clip_id}/waveform",
    "/api/v1/settings",
    "/api/v1/cache",
    "/api/v1/cache/clear",
    "/api/v1/cache/preview",
    "/api/v1/cache/preview/clear",
    "/api/v1/reset-options",
    "/api/v1/reset-backups",
    "/api/v1/reset",
    "/api/v1/reset/restore",
    "/api/v1/adjustment-defaults",
    "/api/v1/session",
    "/api/v1/session/new",
    "/api/v1/session/load",
    "/api/v1/session/save",
    "/api/v1/mounts/unmount-all",
    "/api/v1/library",
    "/api/v1/opened-clips",
    "/api/v1/opened-clips/remove",
    "/api/v1/clips/{clip_id}/annotations",
    "/api/v1/clips/{clip_id}",
    "/api/v1/clips/{clip_id}/thumbnail",
    "/api/v1/clips/{clip_id}/skim",
    "/api/v1/clips/{clip_id}/recipe",
    "/api/v1/clips/{clip_id}/white-balance",
    "/api/v1/clips/{clip_id}/white-balance/validate",
    "/api/v1/clips/{clip_id}/vignette-profile",
    "/api/v1/clips/{clip_id}/analytics",
    "/api/v1/clips/{clip_id}/analytics-scan",
    "/api/v1/clips/{clip_id}/shading-map",
    "/api/v1/clips/{clip_id}/capture.csv",
    "/api/v1/clips/{clip_id}/preview-overlays",
    "/api/v1/clips/{clip_id}/kept-frame",
    "/api/v1/clips/{clip_id}/timing",
    "/api/v1/clips/{clip_id}/gyro",
    "/api/v1/clips/report",
    "/api/v1/clips/{clip_id}/checks",
    "/api/v1/clips/{clip_id}/motion",
    "/api/v1/clips/{clip_id}/raw-histogram",
    "/api/v1/clips/{clip_id}/sensor-clipping",
    "/api/v1/clips/{clip_id}/analysis",
    "/api/v1/clips/trash",
}


def test_the_openapi_document_lists_every_route(client: TestClient) -> None:
    document = client.get(f"{API}/openapi.json").json()
    assert set(document["paths"]) == ROUTES
    assert client.get("/docs").status_code == 404  # the Swagger page loads scripts from a CDN
    assert client.get("/redoc").status_code == 404


def test_health_gives_the_version(client: TestClient) -> None:
    health = client.get(f"{API}/health").json()
    assert health["version"] == __version__


def test_health_gives_the_build(client: TestClient) -> None:
    from mcrawsome.build import build_id

    assert client.get(f"{API}/health").json()["build"] == build_id()
    assert len(build_id()) == 12


def test_doctor_and_hot_folders(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get(f"{API}/looks").status_code == 404
    monkeypatch.setattr(app_module, "run_checks", lambda: [Check(name="FFmpeg", status="ok", detail="7.1")])
    assert client.get(f"{API}/doctor").json() == {
        "checks": [{"name": "FFmpeg", "status": "ok", "detail": "7.1", "fix": None}]
    }
    assert client.get(f"{API}/hot-folders").status_code == 404


def test_the_config_file_is_read_then_the_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert load_config({}) == ServerConfig()
    path = tmp_path / "config" / "mcrawsome" / "server.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'port = 9000\nroots = ["/videos"]\nworkers = 3\n[[hot_folders]]\npath = "/sync/phone"\nto = "exr"\n'
    )
    config = load_config({"port": 9100, "token": None, "roots": []})
    assert config.port == 9100
    assert config.host == "127.0.0.1"
    assert config.roots == [Path("/videos")]
    assert config.workers == 3


def test_the_server_command_passes_its_options(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    served: list[tuple[Any, ServerConfig]] = []
    monkeypatch.setattr(app_module, "serve", lambda app, config: served.append((app, config)))
    recipe = tmp_path / "phone.toml"
    recipe.write_text('look = "gorgeous"\n')
    result = CliRunner().invoke(
        cli,
        ["server", "--port", "9200", "--library", str(library), "--workers", "2"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    [(app, config)] = served
    assert app.state.config == config
    assert (config.host, config.port) == ("127.0.0.1", 9200)
    assert config.roots == [library]
    assert config.workers == 2
    assert "http://127.0.0.1:9200/api/v1" in result.output


def test_an_empty_token_in_the_config_file_is_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "config" / "mcrawsome" / "server.toml"
    path.parent.mkdir(parents=True)
    path.write_text('host = "0.0.0.0"\ntoken = ""\n')
    config = load_config({})
    assert config.token is None
    with pytest.raises(McError) as caught:
        create_app(config)
    assert caught.value.code == "host-not-allowed"


@pytest.mark.parametrize(("code", "status"), [("disk-full", 507), ("engine-busy", 409), ("paused", 409)])
def test_error_codes_have_their_http_status(code: ErrorCode, status: int) -> None:
    from mcrawsome.server.app import _error

    assert _error(code, "x").status_code == status


@pytest.mark.parametrize("path", ["jobs", "jobs/old", "queue/pause", "queue/resume", "plan", "hot-folders"])
def test_retired_export_apis_are_absent(client: TestClient, path: str) -> None:
    assert client.get(f"{API}/{path}").status_code == 404
    assert client.post(f"{API}/{path}", json={}).status_code in (404, 405)


def test_engine_starts_only_the_library_watcher(client: TestClient) -> None:
    names = {thread.name for thread in threading.enumerate()}
    assert "library" in names
    assert not {"queue", "hot-folders"} & names
    assert "runner_pid" not in client.get(f"{API}/health").json()
    assert "queue_paused" not in client.get(f"{API}/health").json()


@pytest.mark.parametrize("enabled", [True, False])
def test_start_up_mounts_sent_media_again_only_when_asked(
    library: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from mcrawsome.mount.manager import MountManager
    from mcrawsome.server.settings import InterfaceSettings, save_settings

    save_settings(InterfaceSettings(remount_on_start=enabled))
    monkeypatch.setattr(app_module, "availability", lambda: (True, ""))
    monkeypatch.setattr(MountManager, "sent_offline", lambda self: ["a" * 24])
    restored = threading.Event()
    monkeypatch.setattr(
        MountManager, "restore", lambda self, ids: restored.set() if ids == ["a" * 24] else None
    )
    with TestClient(create_app(ServerConfig(roots=[library], workers=1)), base_url="http://127.0.0.1:8642"):
        assert restored.wait(5 if enabled else 0.2) is enabled
