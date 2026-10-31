"""On-demand diagnostics for the person using the local desktop or browser."""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from mcrawsome.errors import McError
from mcrawsome.server.config import is_loopback
from mcrawsome.support import diagnostic_members


class SupportReport(BaseModel):
    files: dict[str, str]


def support_router(prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.post("/support/report")
    def report(request: Request, response: Response) -> SupportReport:
        if request.client is None or not is_loopback(request.client.host):
            raise McError("Only this computer may collect support diagnostics", code="client-not-allowed")
        response.headers["Cache-Control"] = "no-store"
        return SupportReport(files=diagnostic_members())

    return router
