"""Clip lists for productions and other software: camera reports and recorded positions."""

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from mcrawsome.errors import McError
from mcrawsome.library.reports import Report, camera_report, locations
from mcrawsome.library.scan import ClipSummary
from mcrawsome.server.routes.errors import ERROR_RESPONSES


class ReportRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=4096, description="The clips, in the order to list them")
    format: Literal["csv", "ale", "gpx"] = Field(
        description="csv or ale: a camera report; gpx: the recorded positions"
    )


def reports_router(prefix: str, find: Callable[[str], ClipSummary]) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.post("/clips/report", responses=ERROR_RESPONSES)
    def report(request: ReportRequest) -> Report:
        """A camera report (CSV or Avid ALE) or a GPX file of the chosen ready clips; the page saves it."""
        clips = [clip for clip in (find(identity) for identity in request.ids) if clip.status == "ready"]
        if not clips:
            raise McError("Choose at least one ready clip", code="input-missing")
        if request.format == "gpx":
            if not any(clip.position for clip in clips):
                raise McError("None of the chosen clips recorded a GPS position", code="input-missing")
            return locations(clips)
        return camera_report(clips, request.format)

    return router
