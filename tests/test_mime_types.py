"""The desktop's file types: .mcraw recordings, and session files, which must not claim other JSON files."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mcrawsome.packaging import packaging_dir


def test_session_files_and_only_they_open_as_mcrawsome_sessions(tmp_path: Path) -> None:
    update, gio = shutil.which("update-mime-database"), shutil.which("gio")
    if update is None or gio is None:
        pytest.skip("needs shared-mime-info's update-mime-database and GLib's gio")
    share = tmp_path / "share"
    (share / "mime/packages").mkdir(parents=True)
    shutil.copy(packaging_dir() / "linux/mcrawsome-mime.xml", share / "mime/packages/mcrawsome.xml")
    subprocess.run([update, str(share / "mime")], check=True, capture_output=True, timeout=60)
    environment = {
        **os.environ,
        "XDG_DATA_HOME": str(tmp_path / "home"),
        "XDG_DATA_DIRS": f"{share}:{os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}",
    }
    expected = {
        "Shoot.mcrawsome-session.json": "application/x-mcrawsome-session",
        "SHOOT.MCRAWSOME-SESSION.JSON": "application/x-mcrawsome-session",
        "notes.json": "application/json",
        "shoot-session.json": "application/json",
        "mcrawsome.json": "application/json",
        "clip.mcraw": "video/x-motioncam-raw",
    }
    for name in expected:
        (tmp_path / name).write_text("{}\n")
    found = {}
    for name in expected:
        result = subprocess.run(
            [gio, "info", "-a", "standard::content-type", str(tmp_path / name)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        found[name] = result.stdout.rsplit("standard::content-type:", 1)[-1].strip()
    assert found == expected
