"""The actual Debian entry script must support spawned RAW preview workers."""

import importlib.util
import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from websockets.sync.client import connect

SPEC = importlib.util.spec_from_file_location(
    "build_deb", Path(__file__).parents[1] / "packaging/deb/build_deb.py"
)
assert SPEC
assert SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.mark.parametrize("name", ["__main__", "__mp_main__"])
def test_launcher_enters_cli_only_for_the_original_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("mcrawsome.cli.main.main", lambda: calls.append(True))
    launcher = tmp_path / "mcrawsome"
    launcher.write_text(builder.launcher_source())
    runpy.run_path(str(launcher), run_name=name)
    assert calls == ([True] if name == "__main__" else [])


def test_server_started_by_debian_launcher_decodes_raw_playback(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    source = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(12)])
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "mcrawsome"
    config.mkdir(parents=True, exist_ok=True)
    (config / "interface.toml").write_text("preview_gpu = false\npreview_cache_mib = 0\n")
    packaged = os.environ.get("MCRAWSOME_TEST_DEB_LAUNCHER")
    launcher = Path(packaged) if packaged else tmp_path / "mcrawsome"
    if not packaged:
        launcher.write_text(builder.launcher_source())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/api/v1"
    output = tmp_path / "engine.log"
    with output.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(launcher), "server", "--port", str(port), "--library", str(tmp_path)],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    with urllib.request.urlopen(f"{url}/health", timeout=1) as response:
                        assert json.load(response)["pid"] == process.pid
                    break
                except OSError:
                    assert process.poll() is None, output.read_text()
                    assert time.monotonic() < deadline, output.read_text()
                    time.sleep(0.05)
            path = urllib.parse.quote(str(source))
            with connect(
                f"ws://127.0.0.1:{port}/api/v1/clips/local/preview?path={path}&width=128&fast_width=128"
            ) as ws:
                opened = json.loads(ws.recv(timeout=15))
                assert opened["kind"] == "opened", (opened, output.read_text())
                ws.send(json.dumps({"seq": 1, "op": "frame", "index": 0, "playback": True, "ahead": 3}))
                frames: list[int] = []
                while len(frames) < 3:
                    header: dict[str, Any] = json.loads(ws.recv(timeout=15))
                    assert header["kind"] != "error", header
                    if header["bytes"]:
                        image = ws.recv(timeout=15)
                        assert isinstance(image, bytes)
                        assert image.startswith(b"\xff\xd8")
                    if header["kind"] == "draft":
                        frames.append(header["index"])
                assert frames == [0, 1, 2]
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
    assert "address already in use" not in output.read_text()
