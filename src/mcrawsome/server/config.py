"""The server's settings: `paths.config_dir()/server.toml` when present, then the command's options."""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from mcrawsome.errors import McError
from mcrawsome.paths import config_dir
from mcrawsome.runtime import default_workers

UserPath = Annotated[Path, AfterValidator(lambda path: path.expanduser())]


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8642, ge=1, le=65535)
    token: str | None = None
    roots: list[UserPath] = Field(default_factory=list)  # empty: the current folder
    workers: int = Field(default_factory=default_workers, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _retired_options(cls, data: Any) -> Any:
        # Old server files remain readable; retired export settings have no effect.
        return (
            {k: v for k, v in data.items() if k not in {"hot_folders", "thermal_limit"}}
            if isinstance(data, dict)
            else data
        )

    @field_validator("token")
    @classmethod
    def _empty_is_none(cls, token: str | None) -> str | None:
        return token or None  # an empty token is no token, whichever way it was given

    def library(self) -> list[Path]:
        """The folders that clips and outputs given to the API must lie in, symlinks resolved."""
        roots = self.roots or [Path.cwd()]
        return [path.resolve() for path in roots]


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


UNENCRYPTED = (
    "the traffic is not encrypted: use a private network (WireGuard, Tailscale) or an HTTPS reverse proxy"
)


def check_exposure(config: ServerConfig) -> None:
    """Refuse to listen beyond this machine without a token."""
    if not is_loopback(config.host) and not config.token:
        raise McError(
            f"listening on {config.host} lets other machines reach the API: give --token as well, so that "
            f"every request must carry it; {UNENCRYPTED}",
            code="host-not-allowed",
        )


def config_path() -> Path:
    return config_dir() / "server.toml"


def load_config(options: dict[str, Any]) -> ServerConfig:
    """The settings file's values, replaced by each option that was given (not None, not an empty list)."""
    path = config_path()
    try:
        settings = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        settings |= {key: value for key, value in options.items() if value is not None and value != []}
        return ServerConfig.model_validate(settings)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise McError(f"{path}: {exc}", code="input-unreadable") from None
