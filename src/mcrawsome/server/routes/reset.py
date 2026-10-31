"""Independent, backed-up resets for defaults, preferences and the current workspace."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Literal

import tomli_w
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from mcrawsome.errors import McError
from mcrawsome.io.atomic import parent_lock, write_atomic
from mcrawsome.library.sessions import Sessions, SessionState
from mcrawsome.paths import state_dir
from mcrawsome.recipe.defaults import defaults_path
from mcrawsome.server.config import is_loopback
from mcrawsome.server.settings import InterfaceSettings, load_settings, settings_path

Scope = Literal["defaults", "preferences", "workspace"]
_DESCRIPTIONS: dict[Scope, str] = {
    "defaults": "Reset application adjustment defaults. Folder and clip overrides are preserved.",
    "preferences": (
        "Reset interface preferences, watched folders and destinations. "
        "Named sessions and recordings are preserved."
    ),
    "workspace": "Start an empty workspace. Named session files and recent-session history are preserved.",
}


class ResetView(BaseModel):
    scope: Scope
    version: str
    description: str


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope
    version: str


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: str


class ResetResult(BaseModel):
    scope: Scope
    version: str
    backup: str
    backup_path: str


def _content(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _version(content: bytes | None) -> str:
    return "" if content is None else hashlib.sha256(content).hexdigest()


def reset_router(prefix: str, sessions: Sessions) -> APIRouter:
    router = APIRouter(prefix=prefix)
    lock = threading.RLock()

    def local(request: Request) -> None:
        if request.client is None or not is_loopback(request.client.host):
            raise McError("Only this computer may reset settings", code="client-not-allowed")

    def path(scope: Scope) -> Path:
        return {"defaults": defaults_path(), "preferences": settings_path(), "workspace": sessions.record}[
            scope
        ]

    def refresh(scope: Scope, request: Request) -> None:
        if scope == "preferences":
            settings, problem = load_settings()
            request.app.state.settings, request.app.state.settings_problem = settings, problem
        elif scope == "workspace":
            raw = _content(sessions.record)
            sessions.state = SessionState.model_validate_json(raw) if raw else SessionState()
            sessions.opened.paths = sessions.state.document.workspace.paths.copy()
            sessions.problem = None

    @router.get("/reset-options")
    def options(request: Request) -> list[ResetView]:
        local(request)
        return [
            ResetView(scope=scope, version=_version(_content(path(scope))), description=description)
            for scope, description in _DESCRIPTIONS.items()
        ]

    @router.get("/reset-backups")
    def backups(request: Request) -> list[ResetResult]:
        local(request)
        result = []
        folder = state_dir() / "reset-backups"
        for backup in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
            if len(backup.stem) != 32 or any(c not in "0123456789abcdef" for c in backup.stem):
                continue
            try:
                payload = json.loads(backup.read_bytes())
                entry = ResetResult(
                    scope=payload["scope"],
                    version=payload["version_after"],
                    backup=backup.stem,
                    backup_path=str(backup),
                )
                if entry.version == _version(_content(path(entry.scope))):
                    result.append(entry)
            except (OSError, ValueError, KeyError):
                continue
        return result[:20]

    @router.post("/reset")
    def reset(update: ResetRequest, request: Request) -> ResetResult:
        local(request)
        # Use the session lock too: a workspace save must not interleave with its backup/replacement.
        with lock, sessions.lock, parent_lock(path(update.scope)):
            target = path(update.scope)
            content = _content(target)
            if _version(content) != update.version:
                raise McError(
                    "Settings changed; refresh reset options before resetting", code="recipe-changed"
                )
            if update.scope == "defaults":
                replacement = b""
            elif update.scope == "preferences":
                replacement = tomli_w.dumps(
                    InterfaceSettings().model_dump(mode="json", exclude_none=True)
                ).encode()
            else:
                replacement = SessionState(recent=sessions.state.recent).model_dump_json(indent=2).encode()
            key = uuid.uuid4().hex
            backup = state_dir() / "reset-backups" / f"{key}.json"
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = {
                "scope": update.scope,
                "version_after": _version(replacement),
                "content": base64.b64encode(content).decode() if content is not None else None,
            }
            write_atomic(backup, json.dumps(payload).encode())
            target.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(target, replacement)
            refresh(update.scope, request)
            return ResetResult(
                scope=update.scope, version=_version(replacement), backup=key, backup_path=str(backup)
            )

    @router.post("/reset/restore")
    def restore(update: RestoreRequest, request: Request) -> ResetResult:
        local(request)
        with lock, sessions.lock:
            backup = state_dir() / "reset-backups" / f"{update.backup}.json"
            try:
                payload = json.loads(backup.read_bytes())
            except FileNotFoundError:
                raise McError("Reset backup is unavailable", code="input-missing") from None
            scope = ResetView(scope=payload["scope"], version="", description="").scope
            with parent_lock(path(scope)):
                target = path(scope)
                if _version(_content(target)) != update.version:
                    raise McError(
                        "Settings changed after reset; restore the backup manually to preserve newer edits",
                        code="recipe-changed",
                    )
                content = (
                    base64.b64decode(payload["content"], validate=True)
                    if payload["content"] is not None
                    else None
                )
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    write_atomic(target, content)
                refresh(scope, request)
                return ResetResult(
                    scope=scope, version=_version(content), backup=update.backup, backup_path=str(backup)
                )

    return router
