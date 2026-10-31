"""Reclaimable disk-cache controls, independent of recipes, mounts and preview leases."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from mcrawsome.server.cache import CacheKind, CacheUsage, PreviewCacheUsage, clear, usage

router = APIRouter()


class CacheClear(BaseModel):
    kind: CacheKind


@router.get("/cache")
def get_cache() -> list[CacheUsage]:
    return usage()


@router.post("/cache/clear")
def clear_cache(request: CacheClear) -> CacheUsage:
    return clear(request.kind)


async def _preview_cache(request: Request, *, clear_frames: bool = False) -> PreviewCacheUsage:
    result = await request.app.state.previews.cache_usage(clear=clear_frames)
    if result is None:
        return PreviewCacheUsage(
            active=False, bytes=0, linear_bytes=0, jpeg_bytes=0, limit_bytes=0, entries=0
        )
    return PreviewCacheUsage(active=True, **result)


@router.get("/cache/preview")
async def get_preview_cache(request: Request) -> PreviewCacheUsage:
    return await _preview_cache(request)


@router.post("/cache/preview/clear")
async def clear_preview_cache(request: Request) -> PreviewCacheUsage:
    return await _preview_cache(request, clear_frames=True)
