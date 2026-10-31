from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome.server.app import create_app
from mcrawsome.server.config import ServerConfig


def test_idle_engine_stop_signals_shutdown() -> None:
    app = create_app(ServerConfig())
    with TestClient(app, base_url="http://127.0.0.1:8642", client=("::1", 5000)) as client:
        assert client.post("/api/v1/engine/stop").status_code == 200
        assert app.state.stopping.is_set()


def test_engine_stop_is_loopback_only() -> None:
    app = create_app(ServerConfig())
    with TestClient(app, base_url="http://127.0.0.1:8642", client=("192.168.1.5", 5000)) as client:
        response = client.post("/api/v1/engine/stop")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "client-not-allowed"
        assert not app.state.stopping.is_set()


def test_stop_endpoint_really_exits_the_detached_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    from mcrawsome import gui

    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = gui.start_server(port, [])
    try:
        assert gui.answers(port)
        assert gui.request(port, "/engine/stop", "POST") == {"stopping": True}
        assert server.wait(timeout=15) == 0
        assert not gui.answers(port, timeout=0.2)
        assert (tmp_path / "state" / "server.log").is_file()
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=15)


def test_preview_and_analysis_shutdown_without_leaked_semaphores(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    import subprocess
    import sys

    clip = make_mcraw(tmp_path / "tiny.mcraw", [0, 41_666_667, 83_333_334])
    program = """
import sys, time, os
from pathlib import Path
from fastapi.testclient import TestClient
from mcrawsome.server.app import create_app
from mcrawsome.server.config import ServerConfig

clip = Path(sys.argv[1])
os.utime(clip, (time.time() - 60,) * 2)
app = create_app(ServerConfig(roots=[clip.parent], workers=1))
with TestClient(app, base_url="http://127.0.0.1:8642") as client:
    with client.websocket_connect(f"ws://127.0.0.1:8642/api/v1/clips/local/preview?path={clip}") as ws:
        assert ws.receive_json()["kind"] == "opened"
        ws.send_json({"seq": 1, "op": "frame", "index": 0})
        while True:
            header = ws.receive_json()
            if header["bytes"]:
                ws.receive_bytes()
            if header["kind"] == "draft":
                break
    client.put("/api/v1/settings", json={"folders": [str(clip.parent)], "workers": 1})
    listed = client.get("/api/v1/library").json()["clips"]
    clip_id = listed[0]["id"]
    client.get(f"/api/v1/clips/{clip_id}/analysis")
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(clip)], capture_output=True, text=True, timeout=150
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "leaked semaphore" not in result.stderr, result.stderr
    assert "ResourceWarning" not in result.stderr, result.stderr
    assert "Exception ignored" not in result.stderr, result.stderr
