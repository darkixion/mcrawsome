"""Per-user desktop integration for an AppImage."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from mcrawsome.appimage import host_environment, running_image
from mcrawsome.packaging import packaging_dir

# Session files show MCRAWsome's icon; shared-mime-info names it after their type.
SESSION_ICON = Path("icons/hicolor/scalable/mimetypes/application-x-mcrawsome-session.svg")


def data_home() -> Path:
    """$XDG_DATA_HOME, which the XDG specification ignores unless it is an absolute path."""
    value = os.environ.get("XDG_DATA_HOME", "")
    return Path(value) if os.path.isabs(value) else Path.home() / ".local/share"


def _registration(image: str) -> dict[Path, bytes]:
    # Desktop Entry string escaping precedes Exec's double-quote escaping.
    quoted = image.replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        quoted = quoted.replace(character, "\\" + character)
    quoted = quoted.replace("\\", "\\\\")
    source = packaging_dir() / "linux"
    template = (source / "mcrawsome.desktop").read_text()
    data = data_home()
    return {
        data / "applications/mcrawsome.desktop": template.replace(
            "Exec=mcrawsome", f'Exec=/usr/bin/env -- "{quoted}"'
        ).encode(),
        data / "icons/hicolor/scalable/apps/mcrawsome.svg": (source / "mcrawsome.svg").read_bytes(),
        data / SESSION_ICON: (source / "mcrawsome.svg").read_bytes(),
        data / "mime/packages/mcrawsome.xml": (source / "mcrawsome-mime.xml").read_bytes(),
    }


def desktop_entry_status() -> tuple[str, bool] | None:
    """The offer identity and whether this image's menu/MIME registration is already current.

    Include the destination and expected files so upgrades from menu-only launchers, moved images,
    deleted registrations and future integration changes can be offered even after an older prompt.
    """
    image = running_image()
    if not image or not Path(image).is_absolute() or not Path(image).is_file():
        return None
    if any(character in image for character in "\n\r="):
        return None
    files = _registration(image)
    identity = hashlib.sha256()
    for path, contents in files.items():
        identity.update(os.fsencode(path) + b"\0" + contents + b"\0")
    try:
        current = all(path.read_bytes() == contents for path, contents in files.items())
        current = current and "*.mcraw" in (data_home() / "mime/globs2").read_text()
        current = (
            current
            and "video/x-motioncam-raw=mcrawsome.desktop;"
            in (data_home() / "applications/mimeinfo.cache").read_text()
        )
    except (OSError, UnicodeError):
        current = False
    return identity.hexdigest(), current


def desktop_entry(remove: bool = False) -> Path:
    if os.geteuid() == 0:
        raise ValueError("desktop-entry is per user: run it as yourself, not as root")
    image = running_image()
    if not remove:
        if not image or not Path(image).is_absolute() or not Path(image).is_file():
            raise ValueError("desktop-entry works only when run from the AppImage")
        if any(character in image for character in "\n\r="):
            raise ValueError("the AppImage's path must not contain a line break or equals sign")
    data = data_home()
    entry = data / "applications/mcrawsome.desktop"
    icon = data / "icons/hicolor/scalable/apps/mcrawsome.svg"
    mime = data / "mime/packages/mcrawsome.xml"
    environment = host_environment()
    commands: list[list[str]] = []
    for tool, folder in (
        ("update-mime-database", mime.parent.parent),
        ("update-desktop-database", entry.parent),
    ):
        if remove and not folder.exists():
            continue
        executable = shutil.which(tool, path=environment.get("PATH", os.defpath))
        if executable is None:
            raise ValueError(
                f"desktop integration needs {tool}; install shared-mime-info and desktop-file-utils"
            )
        commands.append([executable, str(folder)])
    if remove:
        entry.unlink(missing_ok=True)
        icon.unlink(missing_ok=True)
        (data / SESSION_ICON).unlink(missing_ok=True)
        mime.unlink(missing_ok=True)
    else:
        assert image is not None
        # GLib checks the executable before expanding %% in Exec. A fixed executable lets an image
        # whose own filename contains % launch correctly too. env receives the path as one argument.
        for path, contents in _registration(image).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
    for command in commands:
        try:
            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise ValueError(f"{Path(command[0]).name} did not finish within 30 seconds") from None
        if result.returncode:
            raise ValueError(f"{Path(command[0]).name} failed: {result.stderr.strip()}")
    return entry
