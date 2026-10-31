from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import WebSocket
from fastapi.testclient import TestClient
from httpx2 import Response
from starlette.testclient import WebSocketDenialResponse

from mcrawsome.cli.main import cli
from mcrawsome.errors import McError
from mcrawsome.server.app import create_app
from mcrawsome.server.config import ServerConfig

API = "/api/v1"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A clip outside the library."""
    folder = tmp_path / "outside"
    folder.mkdir()
    clip = folder / "a.mcraw"
    clip.write_bytes(b"x")
    return clip


def _refused(response: Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_a_foreign_host_is_refused(client: TestClient) -> None:
    _refused(client.get(f"{API}/health", headers={"Host": "evil.example"}), 400, "host-not-allowed")
    _refused(client.get(f"{API}/health", headers={"Host": "127.0.0.1"}), 400, "host-not-allowed")
    for host in ("127.0.0.1:8642", "localhost:8642", "[::1]:8642"):
        assert client.get(f"{API}/health", headers={"Host": host}).status_code == 200


def test_a_foreign_origin_is_refused(client: TestClient, library: Path) -> None:
    clip = library / "a.mcraw"
    clip.write_bytes(b"x")
    evil = {"Origin": "https://evil.example"}
    _refused(client.get(f"{API}/health", headers=evil), 403, "origin-not-allowed")
    _refused(client.post(f"{API}/session/new", headers=evil), 403, "origin-not-allowed")
    own = {"Origin": "http://127.0.0.1:8642"}
    assert client.get(f"{API}/health", headers=own).status_code == 200


def test_the_pages_own_origin_may_fetch_and_open_a_websocket(library: Path) -> None:
    app = create_app(ServerConfig(roots=[library]))

    @app.websocket("/api/v1/echo")
    async def echo(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_text(await socket.receive_text())
        await socket.close()

    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        own = {"Origin": "http://127.0.0.1:8642"}
        assert client.get(f"{API}/settings", headers=own).status_code == 200
        with client.websocket_connect(f"ws://127.0.0.1:8642{API}/echo", headers=own) as socket:
            socket.send_text("frame")
            assert socket.receive_text() == "frame"
        evil = {"Origin": "https://evil.example"}
        _refused(client.get(f"{API}/settings", headers=evil), 403, "origin-not-allowed")
        with pytest.raises(WebSocketDenialResponse) as denied:
            client.websocket_connect(f"ws://127.0.0.1:8642{API}/echo", headers=evil).__enter__()
        assert denied.value.status_code == 403
        assert denied.value.json()["error"]["code"] == "origin-not-allowed"
        with pytest.raises(WebSocketDenialResponse) as foreign_host:
            client.websocket_connect(
                f"ws://127.0.0.1:8642{API}/echo", headers={"Host": "evil.example"}
            ).__enter__()
        assert foreign_host.value.status_code == 400


def test_listening_beyond_this_machine_needs_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = CliRunner().invoke(cli, ["server", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "--token" in result.output
    assert "not encrypted" in result.output
    with pytest.raises(McError) as caught:
        create_app(ServerConfig(host="0.0.0.0"))
    assert caught.value.code == "host-not-allowed"


def test_with_a_token_every_request_needs_it(library: Path) -> None:
    config = ServerConfig(host="0.0.0.0", token="s3cret", roots=[library])
    with TestClient(create_app(config), base_url="http://192.168.1.5:8642") as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret"}):
            response = client.get(f"{API}/health", headers=headers)
            _refused(response, 401, "unauthorised")
            assert response.headers["www-authenticate"] == "Bearer"
        assert client.get(f"{API}/health", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        foreign = {"Authorization": "Bearer s3cret", "Origin": "https://evil.example"}
        _refused(client.get(f"{API}/health", headers=foreign), 403, "origin-not-allowed")


def test_an_empty_token_is_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert ServerConfig(token="").token is None
    result = CliRunner().invoke(cli, ["server", "--host", "0.0.0.0", "--token", ""])
    assert result.exit_code != 0
    assert "--token" in result.output
    monkeypatch.setenv("MCRAWSOME_SERVER_TOKEN", "")
    result = CliRunner().invoke(cli, ["server", "--host", "0.0.0.0"])
    assert result.exit_code != 0


def test_other_errors_and_unknown_routes_use_the_error_shape(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> list[object]:
        raise RuntimeError("secret detail")

    from mcrawsome.server import app as app_module

    monkeypatch.setattr(app_module, "run_checks", broken)
    app = create_app(ServerConfig(roots=[library]))
    with TestClient(app, base_url="http://127.0.0.1:8642", raise_server_exceptions=False) as client:
        response = client.get(f"{API}/doctor")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal-error"
        assert "secret detail" not in response.text
        missing = client.get(f"{API}/nothing-here")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "input-missing"
        wrong = client.delete(f"{API}/health")
        assert wrong.status_code == 405
        assert wrong.json()["error"]["code"] == "input-unreadable"
