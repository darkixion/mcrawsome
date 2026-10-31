"""The interface's settings: `$XDG_CONFIG_HOME/mcrawsome/interface.toml`, written by the Settings screen.

Its folders extend the server's library roots for opening recordings.
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mcrawsome.io.atomic import parent_lock
from mcrawsome.paths import config_dir, mount_dir
from mcrawsome.resolve import RouteSetting
from mcrawsome.server.config import UserPath

ResolveRoute = RouteSetting

log = logging.getLogger(__name__)


class InterfaceSettings(BaseModel):
    """What the Settings screen sets. A folder that has gone since (an unplugged drive) is kept, and a
    key this version does not know (written by a later one) is ignored."""

    model_config = ConfigDict(extra="ignore")

    folders: list[UserPath] = Field(default_factory=list)
    mount_folder: UserPath | None = None  # None: paths.mount_dir()
    workers: int | None = Field(default=None, ge=1)  # None: the server's analysis worker count
    show_preview_stats: bool = False
    preview_gpu: bool = True
    quad_bayer_remosaic: bool = True
    preview_cache_mib: int = Field(default=256, ge=0, le=2048)
    keep_engine: bool = False  # keep the engine running in the background when the window closes
    remount_on_start: bool = False  # mount sent media again when the engine starts (after a restart)
    resolve_full_resolution: bool = False
    resolve_separate_audio: bool = False
    resolve_link_audio: bool = True
    # Add measured keywords (movement, sustained motion blur) to the keywords Resolve receives.
    resolve_keywords: bool = False
    # Mounted media's timecode: from zero, or the time of day each clip was recorded, on the phone's clock,
    # in this computer's time zone or the recording location's (from its GPS fix).
    timecode: Literal["zero", "time-of-day"] = "zero"
    timecode_zone: Literal["computer", "location"] = "computer"
    resolve_route: ResolveRoute = "auto"  # legacy field: native sending is always available
    # The AppImage's offer of a place in the application menu (gui.offer_desktop_entry): the image and
    # registration it was made for, and the answer. Kept in the file, never shown by the API.
    desktop_entry_offer: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _legacy_studio(cls, data: object) -> object:
        if not isinstance(data, dict) or "resolve_studio" not in data:
            return data
        values = dict(data)
        legacy = values.pop("resolve_studio")
        values.setdefault("resolve_full_resolution", legacy)
        return values

    @field_validator("resolve_route", mode="before")
    @classmethod
    def _native_route(cls, value: object) -> object:
        # Retired scripting routes and the removed enable switch all use native sending.
        return "auto" if isinstance(value, str) and value in ("studio", "menu", "off") else value

    def roots(self) -> list[Path]:
        """The folders and the destination, symlinks resolved."""
        return [path.resolve() for path in self.folders]


class SettingsView(InterfaceSettings):
    """The settings as `GET /settings` gives them, with why the saved file could not be read, if so, and
    where mounts go while no mount folder is set."""

    problem: str | None = Field(default=None, json_schema_extra={"readOnly": True})
    mount_folder_default: str = Field(
        default_factory=lambda: str(mount_dir()), json_schema_extra={"readOnly": True}
    )


class SettingsUpdate(InterfaceSettings):
    """New settings as `PUT /settings` takes them: no unknown key, every path absolute, each folder once.
    The route checks that the folders it adds exist."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _retired_destination(cls, data: object) -> object:
        return (
            {
                k: v
                for k, v in data.items()
                if k not in ("destination", "restore_sent_media", "mount_folder_default")
            }
            if isinstance(data, dict)
            else data
        )

    @field_validator("folders")
    @classmethod
    def _absolute_once(cls, folders: list[Path]) -> list[Path]:
        seen: set[Path] = set()
        kept: list[Path] = []
        for folder in folders:
            if not folder.is_absolute():
                raise ValueError(f"{folder} is not an absolute path")
            if folder.resolve() not in seen:
                seen.add(folder.resolve())
                kept.append(folder)
        return kept

    @field_validator("mount_folder")
    @classmethod
    def _absolute(cls, destination: Path | None) -> Path | None:
        if destination is not None and not destination.is_absolute():
            raise ValueError(f"{destination} is not an absolute path")
        return destination


def settings_path() -> Path:
    return config_dir() / "interface.toml"


def load_settings() -> tuple[InterfaceSettings, str | None]:
    """The saved settings, or the defaults when there are none. A file that cannot be read gives the
    defaults and the reason, logged; it stays as it is until the Settings screen saves new settings."""
    path = settings_path()
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return InterfaceSettings.model_validate(tomllib.loads(text)), None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        problem = f"{path}: {exc}"
        log.warning("the interface settings could not be read; using the defaults: %s", problem)
        return InterfaceSettings(), problem


def save_settings(settings: InterfaceSettings) -> None:
    """Replace the file whole, so a reader never sees it half written."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with parent_lock(path):
        # A live engine may have loaded its settings before the window made the offer.
        saved, _ = load_settings()
        settings = settings.model_copy(
            update={"desktop_entry_offer": settings.desktop_entry_offer or saved.desktop_entry_offer}
        )
        values = settings.model_dump(mode="json", exclude_none=True)
        if settings.desktop_entry_offer is not None:
            values["desktop_entry_offer"] = settings.desktop_entry_offer
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as part:
            tomli_w.dump(values, part)
        os.replace(part.name, path)
