import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.desktop import desktop_entry, desktop_entry_status


@pytest.fixture
def database_tools() -> None:
    if not all(shutil.which(tool) for tool in ("update-mime-database", "update-desktop-database")):
        pytest.skip("needs shared-mime-info and desktop-file-utils")


def test_desktop_entry_uses_appimage_and_removes_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_tools: None
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    image = tmp_path / 'a space "quoted" $image%.AppImage'
    image.touch()
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(image))
    runner = CliRunner()
    result = runner.invoke(cli, ["desktop-entry"])
    assert result.exit_code == 0, result.output
    entry = tmp_path / "data/applications/mcrawsome.desktop"
    text = entry.read_text()
    assert 'Exec=/usr/bin/env -- "' in text
    assert r'a space \\"quoted\\" \\$image%%.AppImage' in text
    assert "Terminal=false" in text
    assert " gui -- %F\n" in text
    assert "MimeType=video/x-motioncam-raw;" in text
    mime = tmp_path / "data/mime/packages/mcrawsome.xml"
    assert "*.mcraw" in mime.read_text()
    assert "video/x-motioncam-raw" in (tmp_path / "data/mime/globs2").read_text()
    assert (
        "video/x-motioncam-raw=mcrawsome.desktop;"
        in (tmp_path / "data/applications/mimeinfo.cache").read_text()
    )
    icon = tmp_path / "data/icons/hicolor/scalable/apps/mcrawsome.svg"
    assert "<svg" in icon.read_text()
    assert runner.invoke(cli, ["desktop-entry", "--remove"]).exit_code == 0
    assert not entry.exists()
    assert not icon.exists()
    assert not mime.exists()
    assert "*.mcraw" not in (tmp_path / "data/mime/globs2").read_text()
    assert "mcrawsome.desktop" not in (tmp_path / "data/applications/mimeinfo.cache").read_text()
    assert runner.invoke(cli, ["desktop-entry", "--remove"]).exit_code == 0


def test_desktop_entry_requires_appimage(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(cli, ["desktop-entry"])
    assert result.exit_code != 0
    assert "AppImage" in result.output


def test_another_appimage_is_not_mcrawsome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A terminal or editor packaged as an AppImage passes APPIMAGE on to everything it starts.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    other = tmp_path / "Terminal.AppImage"
    other.touch()
    monkeypatch.setenv("APPIMAGE", str(other))
    result = CliRunner().invoke(cli, ["desktop-entry"])
    assert result.exit_code != 0
    assert not (tmp_path / "data/applications/mcrawsome.desktop").exists()


def test_desktop_entry_is_never_written_for_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    image = tmp_path / "mcrawsome.AppImage"
    image.touch()
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(image))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    for arguments in (["desktop-entry"], ["desktop-entry", "--remove"]):
        result = CliRunner().invoke(cli, arguments)
        assert result.exit_code != 0
        assert "root" in result.output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("value", ["", "relative/data"])
def test_empty_or_relative_data_home_means_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, database_tools: None
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", value)
    image = tmp_path / "mcrawsome.AppImage"
    image.touch()
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(image))
    result = CliRunner().invoke(cli, ["desktop-entry"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "home/.local/share/applications/mcrawsome.desktop").is_file()
    assert not (tmp_path / "relative").exists()


def test_desktop_launch_passes_an_unmodified_clip_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_tools: None
) -> None:
    """Exercise GLib's actual Desktop Entry parsing and field expansion, without a window or engine."""
    system_python = "/usr/bin/python3"
    probe = subprocess.run([system_python, "-c", "from gi.repository import Gio"], capture_output=True)
    if probe.returncode:
        pytest.skip("needs the desktop's Python GIO bindings")
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    capture = tmp_path / "arguments.json"
    image = tmp_path / 'mcrawsome space "quote" $dollar `tick` %f \\ 雪.AppImage'
    image.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"from pathlib import Path\nPath({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
    )
    image.chmod(0o755)
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(image))
    entry = desktop_entry()
    validator = shutil.which("desktop-file-validate")
    assert validator is not None
    subprocess.run([validator, str(entry)], check=True)
    clip = tmp_path / '--browser $() "quote" %f 雪.MCRAW'
    clip.touch()
    # URI decoding and %f expansion are done by GIO, without a shell interpreting either path.
    driver = (
        "import sys; from gi.repository import Gio; "
        "app = Gio.DesktopAppInfo.new_from_filename(sys.argv[1]); "
        "assert app; assert app.launch([Gio.File.new_for_path(p) for p in sys.argv[2:]], None)"
    )
    second = tmp_path / "second.mcraw"
    second.touch()
    for files in ([str(clip), str(second)], [str(clip)], []):
        capture.unlink(missing_ok=True)
        subprocess.run([system_python, "-c", driver, str(entry), *files], check=True)
        deadline = time.monotonic() + 5
        while not capture.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(capture.read_text()) == ["gui", "--", *files]


def test_registration_preserves_user_defaults_and_other_mime_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_tools: None
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    other = data / "mime/packages/other.xml"
    other.parent.mkdir(parents=True)
    other.write_text(
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">'
        '<mime-type type="video/x-owner-test"><glob pattern="*.owner-test"/></mime-type></mime-info>'
    )
    defaults = tmp_path / "config/mimeapps.list"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    original = "[Default Applications]\nvideo/x-motioncam-raw=other.desktop;\n"
    defaults.write_text(original)
    image = tmp_path / "mcrawsome.AppImage"
    image.touch()
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(image))
    for remove in (False, False, True, True):
        desktop_entry(remove)
        assert defaults.read_text() == original
        assert other.exists()
        assert "*.owner-test" in (data / "mime/globs2").read_text()


def test_registration_requires_database_tools_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    image = tmp_path / "mcrawsome.AppImage"
    image.touch()
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(image))
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="update-mime-database"):
        desktop_entry()
    assert not (tmp_path / "data").exists()


def test_registration_status_detects_legacy_missing_and_moved_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_tools: None
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    image = tmp_path / 'new "image" %.AppImage'
    image.touch()
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(image))
    status = desktop_entry_status()
    assert status is not None
    identity, current = status
    assert not current
    entry = desktop_entry()
    assert desktop_entry_status() == (identity, True)
    text = entry.read_text()
    entry.write_text(text.replace("MimeType=video/x-motioncam-raw;\n", ""))
    assert desktop_entry_status() == (identity, False)
    entry.write_text(text)
    (data / "mime/packages/mcrawsome.xml").unlink()
    assert desktop_entry_status() == (identity, False)
    desktop_entry()
    (data / "applications/mimeinfo.cache").unlink()
    assert desktop_entry_status() == (identity, False)
    desktop_entry()
    moved = tmp_path / "moved.AppImage"
    moved.touch()
    monkeypatch.setenv("MCRAWSOME_APPIMAGE", str(moved))
    moved_status = desktop_entry_status()
    assert moved_status is not None
    assert moved_status[0] != identity
    assert not moved_status[1]
    desktop_entry()
    assert desktop_entry_status() == (moved_status[0], True)
