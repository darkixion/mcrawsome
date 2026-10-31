"""Session files may only be opened or written by this computer."""

from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from mcrawsome.errors import McError
from mcrawsome.library.sessions import Sessions, SessionView, Workspace
from mcrawsome.server.config import is_loopback


class SessionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path


class SessionSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path | None = None
    portable: bool | None = None


def sessions_router(prefix: str, sessions: Sessions) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def local(request: Request) -> None:
        if request.client is None or not is_loopback(request.client.host):
            raise McError("Only this computer may change session files", code="client-not-allowed")

    @router.get("/session")
    def get() -> SessionView:
        return sessions.view()

    @router.put("/session")
    def update(workspace: Workspace, request: Request) -> SessionView:
        local(request)
        return sessions.update(workspace)

    @router.post("/session/new")
    def new(request: Request) -> SessionView:
        local(request)
        return sessions.new()

    @router.post("/session/load")
    def load(file: SessionFile, request: Request) -> SessionView:
        local(request)
        return sessions.load(file.path)

    @router.post("/session/save")
    def save(file: SessionSave, request: Request) -> SessionView:
        local(request)
        return sessions.save(file.path, file.portable)

    return router
