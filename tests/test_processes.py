"""Independent user units survive termination of their installer/desktop/engine parent unit."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcrawsome import processes


def test_desktop_processes_escape_parent_control_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(f"/run/user/{os.getuid()}/bus").exists():
        pytest.skip("User systemd session unavailable")
    monkeypatch.setattr(processes, "systemd_available", lambda: True)
    script = tmp_path / "installer.py"
    metadata = tmp_path / "children.json"
    script.write_text("""import json, subprocess, sys, time
from pathlib import Path
from mcrawsome.processes import spawn, process_id
children = []
for role in ('engine', 'mount', 'resolve'):
    child = spawn([sys.executable, '-c', 'import time;time.sleep(60)'], role=role,
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  start_new_session=True)
    children.append({'pid': process_id(child), 'unit': child.mcrawsome_unit})
Path(sys.argv[1]).write_text(json.dumps(children))
time.sleep(60)
""")
    parent = processes.spawn(
        [sys.executable, str(script), str(metadata)],
        role="engine",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    children = []
    try:
        deadline = time.monotonic() + 10
        while not metadata.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.05)
        assert metadata.exists()
        children = json.loads(metadata.read_text())
        parent_group = Path(f"/proc/{processes.process_id(parent)}/cgroup").read_text()
        for child in children:
            group = Path(f"/proc/{child['pid']}/cgroup").read_text()
            assert child["unit"] in group
            assert group != parent_group
        processes.stop_unit(parent)
        parent.wait(timeout=10)
        for child in children:
            assert Path(f"/proc/{child['pid']}").exists()
    finally:
        processes.stop_unit(parent)
        for child in children:
            subprocess.run(
                ["systemctl", "--user", "stop", child["unit"]], check=False, capture_output=True, timeout=10
            )
        parent.wait(timeout=10)


def test_forking_desktop_handler_keeps_application_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(f"/run/user/{os.getuid()}/bus").exists():
        pytest.skip("User systemd session unavailable")
    monkeypatch.setattr(processes, "systemd_available", lambda: True)
    metadata = tmp_path / "child.pid"
    code = (
        "import subprocess,sys;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "Path(sys.argv[1]).write_text(str(p.pid))"
    )
    handler = processes.spawn(
        [sys.executable, "-c", code, str(metadata)],
        role="resolve",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not metadata.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = int(metadata.read_text())
        assert Path(f"/proc/{child}").exists()
        assert handler.poll() is None
        assert vars(handler)["mcrawsome_unit"] in Path(f"/proc/{child}/cgroup").read_text()
    finally:
        processes.stop_unit(handler)
        handler.wait(timeout=10)
