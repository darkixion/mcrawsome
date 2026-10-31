"""A real editor-style open DNG survives engine replacement and installed-code removal."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import mcrawsome
from mcrawsome.mount.manager import availability

SPEC = importlib.util.spec_from_file_location(
    "lifecycle", Path(__file__).parents[2] / "packaging/deb/package_lifecycle.py"
)
assert SPEC
assert SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def package_copy(root: Path, identity: str) -> Path:
    package = root / "mcrawsome"
    shutil.copytree(
        Path(mcrawsome.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    # Editable source builds put their native modules in a separate site-packages directory.
    for name in ("_fuse", "_raw"):
        module = __import__(f"mcrawsome.{name}", fromlist=[name])
        assert module.__file__
        binary = Path(module.__file__)
        if not (package / binary.name).exists():
            shutil.copy2(binary, package / binary.name)
    vendor = root / "vendor"
    marker = Path(mcrawsome.__file__).with_name("VENDOR")
    if marker.is_file():
        shutil.copytree(Path(marker.read_text().strip()), vendor)
    else:
        vendor.mkdir()
    (package / "MOUNT_RUNTIME").write_text(json.dumps({"id": identity, "protocol": 1, "vendor": str(vendor)}))
    return package


def call(port: int, path: str, body: dict[str, object] | None = None, *, method: str | None = None) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        raise AssertionError(exc.read().decode()) from exc


def test_open_dng_survives_engine_upgrade_and_runtime_replacement(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not availability()[0]:
        pytest.skip("FUSE unavailable")
    old_package = package_copy(tmp_path / "installed-old", "a" * 64)
    new_package = package_copy(tmp_path / "installed-new", "b" * 64)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "mcrawsome"
    config.mkdir(parents=True, exist_ok=True)
    (config / "interface.toml").write_text("restore_sent_media = false\npreview_gpu = false\n")
    script = """import sys
from pathlib import Path
from mcrawsome.mount import runtime
installed_package = Path(sys.argv[1])
runtime._package = lambda: installed_package
from mcrawsome.cli.main import main
sys.argv = ['mcrawsome', 'server', '--port', sys.argv[2], '--library', sys.argv[3]]
main()
"""
    from mcrawsome.mount.manager import MountManager

    manager = MountManager()
    engines: list[subprocess.Popen[bytes]] = []
    records = []

    def launch(package: Path) -> subprocess.Popen[bytes]:
        output = (tmp_path / f"engine-{len(engines)}.log").open("wb")
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(package), str(port), str(tiny_raw.parent)],
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
        finally:
            output.close()
        engines.append(process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None, "engine startup failed"
            try:
                if call(port, "/health")["pid"] == process.pid:
                    return process
            except OSError:
                pass
            time.sleep(0.05)
        pytest.fail("Engine did not become ready")

    try:
        os.utime(tiny_raw, (time.time() - 60,) * 2)
        first = launch(old_package)
        call(port, "/settings", {"folders": [str(tiny_raw.parent)]}, method="PUT")
        clips = call(port, "/library")["clips"]
        [clip] = [item for item in clips if item["path"] == str(tiny_raw)]
        record = call(port, "/mounts", {"clip_id": clip["id"]})
        records.append(record)
        folder = Path(os.environ["MCRAWSOME_STATE_DIR"]) / "mounts" / record["id"]
        ready = json.loads((folder / "ready.json").read_bytes())
        assert ready["runtime"]["id"] == "a" * 64
        service_pid = ready["pid"]
        name = next(path for path in Path(record["path"]).iterdir() if path.suffix == ".dng")
        with name.open("rb") as editor:
            expected = editor.read()
            assert expected
            engine = {
                "pid": first.pid,
                "start": hook.identity(first.pid),
                "uid": os.getuid(),
                "role": "server",
                "args": ["--port", str(port)],
                "env": {key: value for key, value in os.environ.items() if key in hook.ENVIRONMENT},
                "cwd": str(tmp_path),
            }
            service = {
                "pid": service_pid,
                "start": ready["start"],
                "uid": os.getuid(),
                "role": "mount-service",
                "args": [str(folder)],
                "env": {},
                "cwd": str(tmp_path),
            }
            assert hook.preserved_service(service) is not None
            if hasattr(os, "pidfd_open") and hasattr(hook.signal, "pidfd_send_signal"):
                monkeypatch.setattr(hook, "processes", lambda: [engine, service])
                monkeypatch.setattr(hook, "PENDING", tmp_path / "pending/engines.json")  # not this machine's
                hook.stop(remember=True, preserve_mounts=True)
                assert hook.pending()[0]["preserved_mounts"][0]["pid"] == service_pid
            else:
                first.terminate()  # Python3.14 test builds omit pidfd; Debian3.13 exercises real hooks.
            first.wait(timeout=15)
            shutil.rmtree(old_package.parent)  # Model dpkg removing all the old installed files.
            second = launch(new_package)
            [adopted] = [item for item in call(port, "/mounts")["mounts"] if item["id"] == record["id"]]
            assert adopted["active"]
            assert adopted["serving"]
            assert adopted["in_use"]
            assert second.pid != first.pid
            assert hook.identity(service_pid) == ready["start"]
            assert json.loads((folder / "ready.json").read_bytes())["pid"] == service_pid
            editor.seek(0)
            assert editor.read() == expected
            assert name.read_bytes() == expected
            # A new source uses the new package; both generations remain readable together.
            other = tiny_raw.with_name("new-clip.mcraw")
            shutil.copy2(tiny_raw, other)
            from mcrawsome.mount import runtime

            monkeypatch.setattr(runtime, "_package", lambda: new_package)
            new_record = manager.mount(other)
            records.append(new_record.model_dump(mode="json"))
            new_ready = json.loads((folder.parent / new_record.id / "ready.json").read_bytes())
            assert new_ready["runtime"]["id"] == "b" * 64
            assert name.read_bytes() == expected
    finally:
        # Close editor handles before normal unmount; never force a test mount.
        for record in reversed(records):
            with contextlib.suppress(Exception):
                manager.unmount(record["id"])
        for process in engines:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
