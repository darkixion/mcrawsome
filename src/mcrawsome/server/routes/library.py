"""The library's routes: the clips in the interface's folders, one clip's details and its thumbnail.

Clips are named by id (the clip's fingerprint, see library/scan.py). An id that is no longer in the
folders, or whose clip has gone since the last scan, gives 404 `clip-missing`; changes reach the window as
`event: library` on the event stream.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

from mcrawsome.errors import McError
from mcrawsome.library.scan import ClipDetails, ClipSummary, LibraryView
from mcrawsome.library.thumbs import skim, thumbnail
from mcrawsome.library.watch import LibraryWatcher


class ClipSkim(BaseModel):
    frames: list[str]  # base64 JPEGs, at most 320 pixels wide, in order through the clip


def find_clip(clip_id: str, allowed: Callable[[Path], Path], watcher: LibraryWatcher) -> ClipSummary:
    found = watcher.library.find(clip_id)
    if found is None or not found.path.is_file():  # new, moved or gone since the last scan
        watcher.refresh()
        found = watcher.library.find(clip_id)
    if found is None:
        raise McError(f"no clip {clip_id} is in the library folders", code="clip-missing")
    allowed(found.path)
    return found


def library_router(
    api: str,
    allowed: Callable[[Path], Path],
    watcher: LibraryWatcher,
    lookup: Callable[[str], ClipSummary] | None = None,
) -> APIRouter:
    """The routes; `allowed` resolves a path that must lie in the server's library folders (else raises)."""
    router = APIRouter(prefix=api)
    find = lookup or (lambda identity: find_clip(identity, allowed, watcher))

    @router.get("/library")
    def library() -> LibraryView:
        """The clips in the interface's folders, scanned now (unchanged files are not read again)."""
        return watcher.refresh()

    @router.get("/clips/{clip_id}")
    def clip(clip_id: str) -> ClipDetails:
        return watcher.library.details(find(clip_id))

    @router.get(
        "/clips/{clip_id}/thumbnail",
        response_class=Response,
        responses={200: {"content": {"image/jpeg": {}}, "description": "A JPEG at most 320 pixels wide"}},
    )
    def clip_thumbnail(clip_id: str) -> Response:
        found = find(clip_id)
        if found.status != "ready":
            raise McError(found.error or "the clip is not ready", code="input-unreadable")
        assert found.id is not None
        jpeg = thumbnail(found.path, found.id, watcher.library.first_16mib(found.path))
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @router.get("/clips/{clip_id}/skim")
    def clip_skim(clip_id: str) -> ClipSkim:
        """Pictures spread through the clip, made as its thumbnail is, for skimming its card."""
        found = find(clip_id)
        if found.status != "ready":
            raise McError(found.error or "the clip is not ready", code="input-unreadable")
        assert found.id is not None
        frames = skim(found.path, found.id, watcher.library.first_16mib(found.path))
        return ClipSkim(frames=[base64.b64encode(jpeg).decode() for jpeg in frames])

    return router
