"""Default file-manager integration with persistent per-user preferences."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from mcrawsome.appimage import host_environment
from mcrawsome.packaging import packaging_dir
from mcrawsome.paths import data_dir

LAUNCHER = Path("/usr/bin/mcrawsome")
EXTENSIONS_HOME = Path("/usr/share")


def data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME", "")
    return Path(value) if os.path.isabs(value) else Path.home() / ".local/share"


@dataclass(frozen=True)
class Adapter:
    name: str
    label: str
    desktops: tuple[str, ...]
    executable: str
    extension: Path
    asset: Path


ADAPTERS = (
    Adapter(
        "nautilus",
        "GNOME Files (Nautilus)",
        ("gnome", "unity"),
        "nautilus",
        Path("nautilus-python/extensions/mcrawsome_menu.py"),
        Path("file-managers/nautilus/mcrawsome_menu.py"),
    ),
)


class MenuStatus(TypedDict):
    provider: str
    label: str
    supported: bool
    enabled: bool
    current: bool
    requires_admin: bool
    can_install_support: bool
    message: str


def _adapter() -> Adapter | None:
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    return next(
        (
            item
            for item in ADAPTERS
            if any(name in desktops for name in item.desktops)
            and shutil.which(item.executable, path=os.environ.get("PATH", os.defpath))
        ),
        None,
    )


def _support_available(adapter: Adapter) -> bool:
    # Nautilus needs its system Python extension loader as well as our packaged provider.
    return adapter.name == "nautilus" and any(
        path.is_file()
        for path in [
            Path("/usr/lib/nautilus/extensions-4/libnautilus-python.so"),
            Path("/usr/lib64/nautilus/extensions-4/libnautilus-python.so"),
            *Path("/usr/lib").glob("*/nautilus/extensions-4/libnautilus-python.so"),
        ]
    )


def _can_install_support() -> bool:
    release = platform.freedesktop_os_release()
    family = (release.get("ID", "") + " " + release.get("ID_LIKE", "")).split()
    return bool(set(family) & {"debian", "ubuntu"}) and all(
        Path(path).is_file() for path in ("/usr/bin/pkexec", "/usr/bin/apt-get")
    )


def _configuration() -> Path:
    return data_dir() / "file-manager-actions.json"


def menu_status() -> MenuStatus:
    adapter = _adapter()
    status: MenuStatus = {
        "provider": adapter.name if adapter else "",
        "label": adapter.label if adapter else "This desktop",
        "supported": bool(adapter and LAUNCHER.is_file() and os.access(LAUNCHER, os.X_OK)),
        "enabled": False,
        "current": False,
        "requires_admin": False,
        "can_install_support": False,
        "message": "Context menus support GNOME Files (Nautilus) in the Debian desktop application.",
    }
    if adapter is None or not status["supported"]:
        return status
    try:
        configured = json.loads(_configuration().read_text())
        status["enabled"] = configured.get("version") == 3 and configured.get("enabled") is True
    except FileNotFoundError:
        status["enabled"] = True
    except (OSError, ValueError, AttributeError, UnicodeError):
        pass
    extension = EXTENSIONS_HOME / adapter.extension
    with suppress(OSError):
        status["current"] = extension.read_bytes() == (packaging_dir() / adapter.asset).read_bytes()
    status["requires_admin"] = not _support_available(adapter)
    status["can_install_support"] = _can_install_support()
    status["message"] = (
        "Administrator access is required to install Nautilus support (python3-nautilus)."
        if status["requires_admin"]
        else "Enabled by default by the Debian package. You can disable it for your account."
    )
    return status


def _install_support(adapter: Adapter) -> None:
    if adapter.name != "nautilus" or not _can_install_support():
        raise ValueError(
            "Install Nautilus's Python extension support using your system package manager "
            "(administrator access required)."
        )
    result = subprocess.run(
        ["/usr/bin/pkexec", "/usr/bin/apt-get", "install", "-y", "python3-nautilus"],
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode or not _support_available(adapter):
        raise ValueError(
            "Nautilus support was not installed. Administrator authentication may have been cancelled. "
            "No MCRAWsome menu was added."
        )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as part:
        staged = Path(part.name)
        part.write(data)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def set_menu(enabled: bool, *, install_support: bool = False) -> MenuStatus:
    if os.geteuid() == 0:
        raise ValueError("File-manager integration is per user; do not run mcrawsome as root")
    adapter = _adapter()
    if adapter is None:
        raise ValueError("No context-menu adapter is available for this desktop yet")
    if not LAUNCHER.is_file() or not os.access(LAUNCHER, os.X_OK):
        raise ValueError("Install the MCRAWsome Debian package before enabling file-manager integration")
    if enabled and not _support_available(adapter):
        if not install_support:
            raise ValueError(
                "Administrator access is required to install Nautilus support (python3-nautilus)"
            )
        _install_support(adapter)
    # Remove our older per-user provider so it cannot duplicate the packaged provider.
    for item in ADAPTERS:
        (data_home() / item.extension).unlink(missing_ok=True)
    _write(_configuration(), (json.dumps({"version": 3, "enabled": enabled}) + "\n").encode())
    return menu_status()


def file_association() -> bool:
    result = subprocess.run(
        ["/usr/bin/xdg-mime", "query", "default", "video/x-motioncam-raw"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
        env=host_environment(),
    )
    return result.stdout.strip() == "mcrawsome.desktop"


def associate_files() -> bool:
    subprocess.run(
        ["/usr/bin/xdg-mime", "default", "mcrawsome.desktop", "video/x-motioncam-raw"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
        env=host_environment(),
    )
    return file_association()
