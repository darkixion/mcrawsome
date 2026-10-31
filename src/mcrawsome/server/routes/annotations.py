"""Versioned editorial clip details, separate from camera metadata and RAW calibration."""

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from mcrawsome.errors import McError
from mcrawsome.library.annotations import (
    AnnotationRecord,
    ClipAnnotations,
    read_annotations,
    write_annotations,
)
from mcrawsome.library.scan import ClipSummary


class AnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(max_length=64)
    values: ClipAnnotations


def annotations_router(
    prefix: str, find: Callable[[str], ClipSummary], changed: Callable[[Path], None] = lambda _: None
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def ready(identity: str) -> ClipSummary:
        clip = find(identity)
        if clip.status != "ready":
            raise McError("Clip details need a ready recording", code="input-unreadable")
        return clip

    @router.get("/clips/{clip_id}/annotations")
    def get(clip_id: str) -> AnnotationRecord:
        return read_annotations(ready(clip_id).path, clip_id)

    @router.put("/clips/{clip_id}/annotations")
    def put(clip_id: str, request: AnnotationUpdate) -> AnnotationRecord:
        source = ready(clip_id).path
        result = write_annotations(source, clip_id, request.values, request.version)
        changed(source)
        return result

    return router
