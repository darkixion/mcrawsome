"""Where MCRAWsome keeps files of its own: a cache (safe to delete), state (sessions and media mounts),
settings and per-user data."""

from __future__ import annotations

import os
from pathlib import Path

NAME = "mcrawsome"
SESSION_SUFFIX = f".{NAME}-session.json"


def _folder(override: str, xdg: str, fallback: Path) -> Path:
    if os.environ.get(override):
        return Path(os.environ[override])
    return (Path(os.environ[xdg]) if os.environ.get(xdg) else fallback) / NAME


def cache_dir() -> Path:
    """MCRAWSOME_CACHE_DIR, else $XDG_CACHE_HOME/mcrawsome, else ~/.cache/mcrawsome."""
    return _folder("MCRAWSOME_CACHE_DIR", "XDG_CACHE_HOME", Path.home() / ".cache")


def state_dir() -> Path:
    """MCRAWSOME_STATE_DIR, else $XDG_STATE_HOME/mcrawsome, else ~/.local/state/mcrawsome."""
    return _folder("MCRAWSOME_STATE_DIR", "XDG_STATE_HOME", Path.home() / ".local" / "state")


def config_dir() -> Path:
    """$XDG_CONFIG_HOME/mcrawsome, else ~/.config/mcrawsome: settings, defaults and camera profiles."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / NAME


def data_dir() -> Path:
    """$XDG_DATA_HOME/mcrawsome (when absolute, as the XDG specification requires), else
    ~/.local/share/mcrawsome."""
    value = os.environ.get("XDG_DATA_HOME", "")
    return (Path(value) if os.path.isabs(value) else Path.home() / ".local" / "share") / NAME


def mount_dir() -> Path:
    """Where new mounts go unless Settings name a folder: MCRAWSOME_MOUNT_DIR, else ~/Mounts/mcrawsome."""
    if os.environ.get("MCRAWSOME_MOUNT_DIR"):
        return Path(os.environ["MCRAWSOME_MOUNT_DIR"])
    return Path.home() / "Mounts" / NAME
