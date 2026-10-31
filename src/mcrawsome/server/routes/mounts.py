"""Editor-neutral mounted media, with Resolve as the first import adapter."""

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.library.scan import ClipSummary, clip_recipe
from mcrawsome.mount.limits import MAX_MOUNTS
from mcrawsome.mount.manager import MountedSequence, MountListing, MountManager
from mcrawsome.mount.readiness import readable_media
from mcrawsome.mount.sequence import mounted_rate
from mcrawsome.output.export import clip_sources, frame_range_for
from mcrawsome.recipe.clip_layer import read_layer
from mcrawsome.recipe.schema import RawCalibration, Recipe
from mcrawsome.resolve import RouteSetting, native
from mcrawsome.resolve.media_mounts import prepare_media_mounts
from mcrawsome.resolve.timeline_archive import unusable_rates, usable_rate
from mcrawsome.server.progress import ProgressHub


class MountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clip_id: str


class SendMountsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mounts: list[str] = Field(min_length=1, max_length=MAX_MOUNTS)
    mode: native.SendMode = "bin"
    # Accepted from older pages and ignored: a timeline stabilises each clip by its own Stabilise settings.
    gyro: bool = False
    unchecked_gyro: bool = False
    rolling_shutter: bool = False
    # The page's token for this send's stages on the event stream ("send" events).
    progress: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class MediaListing(MountListing):
    """Mounted media available to the interface."""


class TimelineRatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clip_ids: list[str] = Field(min_length=1, max_length=MAX_MOUNTS)


class TimelineRate(BaseModel):
    id: str
    name: str
    rate: float  # frames per second, as the clip mounts by its Mounted frame rate
    usable: bool  # whether a Resolve timeline can take that rate
    nearest: int | None  # the rate Nearest whole rate mounts it at, when a timeline can take that


class TimelineRates(BaseModel):
    clips: list[TimelineRate]
    message: str | None  # why a timeline cannot take some of them, as the send would say; None if it can


class _Rated(NamedTuple):
    stem: str
    rate: float


def marked_range(path: Path, recipe: Recipe) -> tuple[int, int] | None:
    """The mounted frames: the clip's in and out marks ([trim]) in the mount's output frames, or None for
    the whole recording."""
    rate = recipe.develop.mount_frame_rate
    frame_range, _ = frame_range_for(path, recipe.trim.start, recipe.trim.duration, frame_rate=rate)
    if frame_range is None:
        return None
    with Clip(path) as clip:
        total = len(clip_sources(clip, True, rate))
    return None if frame_range == (0, total) else frame_range


class UnmountResult(BaseModel):
    id: str
    source: Path
    ok: bool
    code: str | None = None
    message: str


class UnmountResults(BaseModel):
    results: list[UnmountResult]


def mounts_router(
    prefix: str,
    manager: MountManager,
    find: Callable[[str], ClipSummary],
    route: Callable[[], RouteSetting],
    allowed: Callable[[Path], Path],
    progress: ProgressHub | None = None,
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.get("/mounts")
    def mounts() -> MediaListing:
        return MediaListing(**manager.listing().model_dump())

    @router.post("/mounts", status_code=201)
    def mount(request: MountRequest) -> MountedSequence:
        clip = find(request.clip_id)
        if clip.kind != "raw" or clip.status != "ready":
            raise McError("Virtual CinemaDNG needs a ready RAW clip", code="recipe-invalid")
        recipe = clip_recipe(clip.path, read_layer(clip.path))
        develop = recipe.develop
        return manager.mount(
            clip.path,
            vignette_profile=develop.vignette_profile,
            colour_matrix=recipe.camera.colour_matrix,
            raw_settings=RawCalibration.model_validate(
                {name: getattr(develop, name) for name in RawCalibration.model_fields}
            ),
            frame_range=marked_range(clip.path, recipe),
        )

    @router.post("/resolve/timeline/rates")
    def timeline_rates(request: TimelineRatesRequest) -> TimelineRates:
        """Each clip's rate as it would mount, so a timeline send can stop before mounting anything."""
        rates: list[TimelineRate] = []
        named: list[_Rated] = []
        for identity in request.clip_ids:
            clip = find(identity)
            if clip.kind != "raw" or clip.status != "ready":
                raise McError("Virtual CinemaDNG needs a ready RAW clip", code="recipe-invalid")
            choice = clip_recipe(clip.path, read_layer(clip.path)).develop.mount_frame_rate
            with Clip(clip.path) as recording:
                rate, nearest = (
                    float(mounted_rate(recording, choice)),
                    int(mounted_rate(recording, "integer")),
                )
            usable = usable_rate(rate)
            fixed = nearest if usable_rate(nearest) else None
            rates.append(
                TimelineRate(id=identity, name=clip.name, rate=round(rate, 3), usable=usable, nearest=fixed)
            )
            named.append(_Rated(clip.path.stem, rate))
        return TimelineRates(clips=rates, message=unusable_rates(named))

    @router.post("/mounts/unmount-all")
    def unmount_all() -> UnmountResults:
        results = []
        for record in manager.listing().mounts:
            if not record.active:
                continue
            try:
                manager.unmount(record.id)
                results.append(
                    UnmountResult(id=record.id, source=record.source, ok=True, message="Unmounted")
                )
            except McError as exc:
                results.append(
                    UnmountResult(
                        id=record.id, source=record.source, ok=False, code=exc.code, message=exc.message
                    )
                )
        return UnmountResults(results=results)

    @router.post("/mounts/{mount_id}/remount")
    def remount(mount_id: str) -> MountedSequence:
        allowed(manager.get(mount_id).source)
        return manager.remount(mount_id)

    @router.post("/mounts/{mount_id}/unmount")
    def unmount(mount_id: str) -> MountedSequence:
        return manager.unmount(mount_id)

    @router.post("/resolve/mounts/send")
    def send(request: SendMountsRequest, http_request: Request) -> native.NativeSendResult:
        mounted = []
        for identity in dict.fromkeys(request.mounts):
            mount = manager.get(identity)
            # Recheck the library boundary if folders changed since the sequence was mounted.
            allowed(mount.source)
            if not mount.active or not mount.source_available or not mount.serving:
                raise McError(
                    "Sequence is unmounted or its source is offline; mount it again", code="recipe-invalid"
                )
            mounted.append(mount)
        # Keep the exact saved mount settings as the send snapshot, but serve native
        # archives from recorded-orientation media. Rotation is a Clip Attributes
        # property; applying it in both the DNG tag and the archive would turn it twice.
        timeline_mounts = mounted
        dependencies = mounted
        proxies = None
        rotations = None
        setting = route()
        if setting != "off":
            prepared = prepare_media_mounts(manager, mounted, linked_proxies=True)
            timeline_mounts, rotations = prepared.originals, prepared.rotations
            dependencies, proxies = prepared.dependencies, prepared.proxies
        # Stages are told for the clips the page sent, which a timeline may serve from other media.
        selected = {sent.id: chosen.id for sent, chosen in zip(timeline_mounts, mounted, strict=True)}

        def stage(mount: MountedSequence | None, text: str) -> None:
            if progress is not None and request.progress is not None:
                clip = f"mount-{selected.get(mount.id, mount.id)}" if mount is not None else None
                progress.publish({"send": request.progress, "mount": clip, "stage": text})

        def retain_archive_media() -> None:
            # Gyro analysis can take time: recheck the actual dependencies just
            # before dispatch, including an auxiliary recorded-orientation mount.
            for mount in dependencies:
                readable_media(mount)
            manager.retain([mount.id for mount in dependencies])

        preferences = http_request.app.state.settings
        result = native.send_mounts(
            timeline_mounts,
            setting,
            mode=request.mode,
            rotations=rotations,
            on_archive=retain_archive_media,
            separate_audio=preferences.resolve_separate_audio,
            link_audio=preferences.resolve_link_audio,
            full_resolution=preferences.resolve_full_resolution,
            proxies=proxies,
            progress=stage,
        )
        # UI results belong to the originally selected mount, even when the archive
        # references a shared recorded-orientation mount with a different identity.
        for item, mount in zip(result.results, mounted, strict=True):
            item.id = "mount-" + mount.id
        return result

    return router
