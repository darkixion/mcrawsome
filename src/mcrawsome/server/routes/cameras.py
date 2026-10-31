"""Measured lens profiles: explicit recording analysis, review, then local installation."""

import asyncio
from collections.abc import Callable
from concurrent.futures import CancelledError
from functools import partial
from pathlib import Path
from typing import Annotated

import cv2
from fastapi import APIRouter, Request
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, model_validator

from mcrawsome.analysis.diagnostics import DiagnosticHost
from mcrawsome.camera.profile import (
    CameraProfile,
    ProfileError,
    all_profiles,
    builtin_dir,
    delete_profile,
    save_profile,
)
from mcrawsome.errors import McError
from mcrawsome.io.container import ContainerError
from mcrawsome.io.directlog import DirectLogError
from mcrawsome.library.scan import ClipSummary
from mcrawsome.output.ffmpeg import FfmpegError
from mcrawsome.pipeline.calibrate import CalibrationError
from mcrawsome.pipeline.lens_calibrate import (
    CaptureRecording,
    LensCaptureOptions,
    LensCaptureResult,
    analyse_camera,
    inspect_capture,
)
from mcrawsome.server.config import is_loopback
from mcrawsome.server.routes.errors import ERROR_RESPONSES


class LensCaptureRequest(LensCaptureOptions):
    clip_id: str | None = None
    path: Path | None = None
    gyro_clip_id: str | None = None
    gyro_path: Path | None = None

    @model_validator(mode="after")
    def one_recording(self) -> "LensCaptureRequest":
        if (self.clip_id is None) == (self.path is None):
            raise ValueError("Choose one calibration recording")
        if self.gyro_clip_id is not None and self.gyro_path is not None:
            raise ValueError("Choose one optional gyro recording")
        return self


class CaptureRecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path


class CameraProfileEntry(BaseModel):
    profile: CameraProfile
    user: bool


class CameraProfileList(BaseModel):
    profiles: list[CameraProfileEntry]


class CameraProfileSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: CameraProfile


def _local(request: Request) -> None:
    if request.client is None or not is_loopback(request.client.host):
        raise McError("Only this computer may create camera profiles", code="client-not-allowed")


def cameras_router(prefix: str, find: Callable[[str], ClipSummary], host: DiagnosticHost) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def source(identity: str | None, path: Path | None) -> Path:
        if identity is not None:
            clip = find(identity)
            if clip.kind not in ("raw", "log") or clip.status != "ready":
                raise McError("Choose a ready RAW or video calibration recording", code="recipe-invalid")
            return clip.path
        assert path is not None
        try:
            return Path(inspect_capture(path).path)
        except (ValueError, OSError, RuntimeError, ContainerError, DirectLogError, FfmpegError) as exc:
            raise McError(str(exc), code="input-unreadable") from None

    @router.post("/camera-capture/recordings", responses=ERROR_RESPONSES)
    def recording(body: CaptureRecordingRequest, request: Request) -> CaptureRecording:
        _local(request)
        try:
            return inspect_capture(body.path)
        except (ValueError, OSError, RuntimeError, ContainerError, DirectLogError, FfmpegError) as exc:
            raise McError(str(exc), code="input-unreadable") from None

    @router.get("/camera-capture", responses=ERROR_RESPONSES)
    def listing() -> CameraProfileList:
        try:
            return CameraProfileList(
                profiles=[CameraProfileEntry(profile=p, user=mine) for p, mine in all_profiles()]
            )
        except ProfileError as exc:
            raise McError(str(exc), code="input-unreadable") from None

    @router.post("/camera-capture", responses=ERROR_RESPONSES)
    def save(body: CameraProfileSave, request: Request) -> CameraProfileEntry:
        _local(request)
        profile = body.profile
        lens = profile.lens_calibration
        if lens is None or lens.image_count < 15 or lens.rms_error is None or lens.rms_error > 1.5:
            raise McError(
                "A measured lens with at least 15 views and RMS at most 1.5 px is required",
                code="recipe-invalid",
            )
        if (profile.match.width, profile.match.height) != (lens.width, lens.height):
            raise McError("The camera profile must match its calibration dimensions", code="recipe-invalid")
        if not profile.match.device_model:
            raise McError("Name the camera model before adding its profile", code="recipe-invalid")
        try:
            save_profile(profile)
        except ProfileError as exc:
            raise McError(str(exc), code="output-exists") from None
        except OSError as exc:
            raise McError(f"Could not save the camera profile: {exc}", code="output-unwritable") from None
        return CameraProfileEntry(profile=profile, user=True)

    @router.delete("/camera-capture/{name}", responses=ERROR_RESPONSES)
    def delete(
        name: Annotated[str, ApiPath(pattern=r"^[a-z0-9][a-z0-9_]*$")], request: Request
    ) -> CameraProfileList:
        _local(request)
        try:
            delete_profile(name)
        except FileNotFoundError:
            if (builtin_dir() / f"{name}.toml").is_file():
                raise McError("Built-in camera profiles cannot be deleted", code="path-not-allowed") from None
            raise McError(f"No saved user camera profile named {name}", code="input-missing") from None
        except OSError as exc:
            raise McError(f"Could not delete the camera profile: {exc}", code="output-unwritable") from None
        return listing()

    @router.post("/camera-capture/analyse", responses=ERROR_RESPONSES)
    async def analyse(body: LensCaptureRequest, request: Request) -> LensCaptureResult:
        _local(request)
        path = await asyncio.to_thread(source, body.clip_id, body.path)
        gyro_path = (
            await asyncio.to_thread(source, body.gyro_clip_id, body.gyro_path)
            if body.gyro_clip_id or body.gyro_path
            else None
        )
        if gyro_path is not None and gyro_path.resolve() == path.resolve():
            raise McError(
                "Use a separate distant-scene recording for gyro calibration", code="recipe-invalid"
            )
        options = LensCaptureOptions.model_validate(
            body.model_dump(exclude={"clip_id", "path", "gyro_clip_id", "gyro_path"})
        )
        future, stop = host.submit(partial(analyse_camera, path, options, gyro_path=gyro_path))
        try:
            while not future.done():
                if await request.is_disconnected():
                    raise McError("Lens calibration cancelled", code="paused")
                await asyncio.sleep(0.1)
            return future.result()
        except CancelledError:
            raise McError("Lens calibration cancelled", code="paused") from None
        except (
            ValueError,
            OSError,
            RuntimeError,
            cv2.error,
            ContainerError,
            DirectLogError,
            FfmpegError,
            CalibrationError,
        ) as exc:
            raise McError(f"Could not calibrate {path.name}: {exc}", code="input-unreadable") from None
        finally:
            stop.set()

    return router
