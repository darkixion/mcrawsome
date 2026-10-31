"""Local, explicit camera-profile analysis and persistence."""

import asyncio
from collections.abc import Callable
from concurrent.futures import CancelledError
from functools import partial
from pathlib import Path

import tomli_w
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, model_validator

from mcrawsome.analysis.diagnostics import DiagnosticHost
from mcrawsome.camera.calibrate_lens import LensAnalysis, Model, analyse_lens, target_svg
from mcrawsome.camera.profile import CameraProfile, ProfileError, all_profiles, save_profile
from mcrawsome.errors import McError
from mcrawsome.library.scan import ClipSummary
from mcrawsome.pipeline.calibrate import CalibrationError
from mcrawsome.server.config import is_loopback


class ProfileEntry(BaseModel):
    profile: CameraProfile
    user: bool


class ProfileList(BaseModel):
    profiles: list[ProfileEntry]


class LensRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clip_id: str | None = None
    path: Path | None = None
    model: Model = "opencv_standard"

    @model_validator(mode="after")
    def one_recording(self) -> "LensRequest":
        if (self.clip_id is None) == (self.path is None):
            raise ValueError("Choose one recording")
        return self


class SaveProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: CameraProfile


class SavedProfile(BaseModel):
    path: str
    profile: CameraProfile


def _local(request: Request) -> None:
    if request.client is None or not is_loopback(request.client.host):
        raise McError("Only this computer may create camera profiles", code="client-not-allowed")


def camera_profiles_router(
    prefix: str, find: Callable[[str], ClipSummary], host: DiagnosticHost
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.get("/camera-profiles")
    def listing() -> ProfileList:
        all_profiles.cache_clear()
        try:
            return ProfileList(
                profiles=[ProfileEntry(profile=profile, user=mine) for profile, mine in all_profiles()]
            )
        except ProfileError as exc:
            raise McError(str(exc), code="input-unreadable") from None

    @router.get("/camera-profiles/target.svg")
    def target() -> Response:
        return Response(
            target_svg(),
            media_type="image/svg+xml",
            headers={
                "Content-Disposition": 'attachment; filename="mcrawsome-lens-target.svg"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/camera-profiles/analyse")
    async def analyse(body: LensRequest, request: Request) -> LensAnalysis:
        _local(request)
        if body.clip_id is not None:
            clip = await asyncio.to_thread(find, body.clip_id)
            if clip.kind != "raw" or clip.status != "ready":
                raise McError("Choose a ready MotionCam RAW target recording", code="recipe-invalid")
            source = clip.path
        else:
            assert body.path is not None
            source = body.path.expanduser().resolve()
            if not source.is_file() or source.suffix.lower() != ".mcraw":
                raise McError("Choose a MotionCam RAW target recording", code="input-unreadable")
        future, stop = host.submit(partial(analyse_lens, source, body.model))
        try:
            while not future.done():
                if await request.is_disconnected():
                    raise CancelledError
                await asyncio.sleep(0.05)
            return future.result()
        except CancelledError:
            raise McError("Lens calibration cancelled", code="paused") from None
        except (CalibrationError, ValueError, OSError) as exc:
            raise McError(str(exc), code="input-unreadable") from None
        finally:
            stop.set()

    @router.post("/camera-profiles", status_code=201)
    def save(body: SaveProfile, request: Request) -> SavedProfile:
        _local(request)
        try:
            path = save_profile(body.profile)
        except ProfileError as exc:
            raise McError(str(exc), code="output-exists") from None
        except OSError as exc:
            raise McError(str(exc), code="output-unwritable") from None
        return SavedProfile(path=str(path), profile=body.profile)

    @router.get("/camera-profiles/{name}.toml")
    def download(name: str) -> Response:
        try:
            profiles = all_profiles()
        except ProfileError as exc:
            raise McError(str(exc), code="input-unreadable") from None
        for profile, _mine in profiles:
            if profile.name == name:
                return Response(
                    tomli_w.dumps(profile.model_dump(mode="json", exclude_none=True)),
                    media_type="application/toml",
                    headers={
                        "Content-Disposition": f'attachment; filename="{profile.name}.toml"',
                        "Cache-Control": "no-store",
                    },
                )
        raise McError("Camera profile does not exist", code="input-missing")

    return router
