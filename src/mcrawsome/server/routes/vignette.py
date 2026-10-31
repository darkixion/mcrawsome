"""Metadata-only profile exchange and versioned, non-destructive clip overrides."""

from collections.abc import Callable
from copy import deepcopy

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from mcrawsome.camera.vignette import VignetteProfile, apply_profile, capture_profile
from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.library.scan import ClipSummary, clip_recipe
from mcrawsome.mount.manager import MountManager
from mcrawsome.recipe.clip_layer import ClipLayer, read_layer, write_layer


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    profile: VignetteProfile | None


def check_profile_change(clip: ClipSummary, profile: VignetteProfile | None, mounts: MountManager) -> None:
    if clip.kind != "raw" or clip.status != "ready":
        raise McError("Vignette profiles need a ready RAW clip", code="recipe-invalid")
    if any(mount.active and mount.source == clip.path.resolve() for mount in mounts.listing().mounts):
        raise McError("Unmount this clip before changing its vignette profile", code="engine-busy")
    if profile is not None:
        with Clip(clip.path) as source:
            try:
                apply_profile(source.metadata, source.frame_metadata(0), profile)
            except ValueError as exc:
                raise McError(str(exc), code="recipe-invalid") from None


def vignette_router(prefix: str, find: Callable[[str], ClipSummary], mounts: MountManager) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def raw(identity: str) -> ClipSummary:
        clip = find(identity)
        if clip.kind != "raw" or clip.status != "ready":
            raise McError("Vignette profiles need a ready RAW clip", code="recipe-invalid")
        return clip

    @router.get("/clips/{clip_id}/vignette-profile")
    def export(clip_id: str) -> VignetteProfile:
        clip = raw(clip_id)
        profile = clip_recipe(clip.path, read_layer(clip.path)).develop.vignette_profile
        if profile is not None:
            return profile
        with Clip(clip.path) as source:
            return capture_profile(source.metadata, source.frame_metadata(0))

    @router.put("/clips/{clip_id}/vignette-profile")
    def update(clip_id: str, request: ProfileUpdate) -> ClipLayer:
        clip = raw(clip_id)
        check_profile_change(clip, request.profile, mounts)
        layer = read_layer(clip.path)
        if layer.error:
            raise McError(layer.error, code="recipe-invalid")
        values = deepcopy(layer.values)
        develop = values.setdefault("develop", {})
        if request.profile is None:
            develop.pop("vignette_profile", None)
            if not develop:
                values.pop("develop")
        else:
            develop["vignette_profile"] = request.profile.model_dump(mode="json")
        return write_layer(clip.path, values, request.version)

    return router
