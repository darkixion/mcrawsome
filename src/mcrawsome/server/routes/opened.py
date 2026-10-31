"""Local paths only, never uploaded file contents or saved library folders."""

from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from mcrawsome.errors import McError
from mcrawsome.library.scan import LibraryView
from mcrawsome.library.sessions import Sessions
from mcrawsome.server.config import is_loopback


class OpenFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[Path] = Field(min_length=1, max_length=256)


def opened_router(api: str, sessions: Sessions) -> APIRouter:
    router = APIRouter(prefix=api)

    @router.get("/opened-clips")
    def listing() -> LibraryView:
        return sessions.opened.refresh()

    @router.post("/opened-clips")
    def add(files: OpenFiles, request: Request) -> LibraryView:
        if request.client is None or not is_loopback(request.client.host):
            raise McError("Only this computer may open local files", code="client-not-allowed")
        return sessions.open_files(files.paths)

    @router.delete("/opened-clips")
    def clear(request: Request) -> LibraryView:
        if request.client is None or not is_loopback(request.client.host):
            raise McError("Only this computer may close local files", code="client-not-allowed")
        return sessions.close_files()

    @router.post("/opened-clips/remove")
    def remove(files: OpenFiles, request: Request) -> LibraryView:
        if request.client is None or not is_loopback(request.client.host):
            raise McError("Only this computer may close local files", code="client-not-allowed")
        return sessions.remove_files(files.paths)

    return router
