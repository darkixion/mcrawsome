"""Persistent native archives activated through the desktop's normal file association."""

from __future__ import annotations

import logging
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from mcrawsome.camera.profile import ProfileError
from mcrawsome.errors import McError
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.paths import state_dir
from mcrawsome.processes import spawn
from mcrawsome.recipe.load import RecipeError
from mcrawsome.resolve import RouteSetting
from mcrawsome.resolve.bin_archive import mounted_media, write_bin
from mcrawsome.resolve.timeline_archive import timeline_size, write_timeline

_LOG = logging.getLogger(__name__)

SendMode = Literal["bin", "timeline"]


class OutputResult(BaseModel):
    id: str
    added: bool = False
    waiting: bool = False
    message: str
    archive: Path | None = None
    dispatched: bool | None = None


class NativeSendResult(BaseModel):
    archive: Path | None = None
    archives: list[Path] = Field(default_factory=list)
    launched: bool = False
    results: list[OutputResult] = Field(default_factory=list)


def open_archive(path: Path) -> None:
    # Some desktop handlers stay attached until Resolve closes its import dialogue or exits.
    # A running handler is dispatched, not a failed import. Keep its lifetime separate from the API.
    process = spawn(
        ["xdg-open", str(path)],
        role="resolve",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:

        def reap() -> None:
            code = process.wait()
            if code:
                _LOG.warning("Desktop file handler exited with status %s for %s", code, path)

        threading.Thread(target=reap, name="archive-open", daemon=True).start()
        return
    if code:
        raise OSError(f"Could not open the archive through its file association: exit status {code}")


TimelineKey = tuple[float, tuple[int, int]]  # a timeline's frame rate and displayed size


def timeline_indices(
    mounts: list[MountedSequence], rotations: list[int] | None
) -> list[tuple[list[int], TimelineKey | None]]:
    """The clips for each timeline: a Resolve timeline takes one frame rate, and clips of one displayed
    size share it."""
    if rotations is not None and len(rotations) != len(mounts):
        raise ValueError("Expected one display rotation per clip")
    if len(mounts) < 2:
        return [(list(range(len(mounts))), None)]
    grouped: dict[TimelineKey, list[int]] = {}
    for index, mount in enumerate(mounts):
        media = mounted_media(mount)
        dimensions = timeline_size([media], [rotations[index]] if rotations else None)
        grouped.setdefault((media.rate, dimensions), []).append(index)
    return [(indices, key) for key, indices in grouped.items()]


def send_mounts(
    mounts: list[MountedSequence],
    setting: RouteSetting,
    *,
    mode: SendMode = "bin",
    rotations: list[int] | None = None,
    on_archive: Callable[[], None] | None = None,
    separate_audio: bool = False,
    link_audio: bool = True,
    full_resolution: bool = False,
    proxies: list[MountedSequence | None] | None = None,
    progress: Callable[[MountedSequence | None, str], None] | None = None,
) -> NativeSendResult:
    """`progress` is told each clip's stage as a timeline is prepared, then the send's (clip None)."""
    from mcrawsome.resolve.gyro import wants_stabilisation  # gyro analysis is heavy to import

    result = NativeSendResult()
    paths: dict[int, Path] = {}
    dispatched: set[Path] = set()
    if setting == "off":
        message = "Sending to Resolve is switched off in Settings"
    else:
        try:
            if proxies is not None and len(proxies) != len(mounts):
                raise ValueError("Expected one optional proxy per original clip")
            stabilised_mounts = (
                [mount for mount in mounts if wants_stabilisation(mount.source)] if mode == "timeline" else []
            )
            # Corrections are planned in the recorded frame's pixels; a crop or an unpaired proxy changes
            # them. Proxies sent from the Library are linked to full-resolution originals, which carry them.
            if any(mount.raw_settings.mount_crop != "none" for mount in stabilised_mounts):
                raise ValueError(
                    "Stabilised clips need uncropped media; set Mounted DNG crop to None, "
                    "or switch Stabilise off for those clips in Resolve Settings, and send again"
                )
            if any(mount.raw_settings.mount_proxy != "full" for mount in stabilised_mounts):
                raise ValueError(
                    "Stabilised clips need their full-resolution originals; send them from the Library "
                    "so their proxies are linked to the originals"
                )
            folder = state_dir() / "send/resolve-archives" / uuid.uuid4().hex
            folder.mkdir(parents=True, mode=0o700)
            name = "MCRAWsome " + datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            groups = (
                timeline_indices(mounts, rotations)
                if mode == "timeline"
                else [(list(range(len(mounts))), None)]
            )
            keys = [key for _, key in groups if key is not None]
            by_rate = len({rate for rate, _ in keys}) > 1
            by_size = len({size for _, size in keys}) > 1
            stabilised = 0
            for indices, key in groups:
                group = [mounts[index] for index in indices]
                turns = [rotations[index] for index in indices] if rotations is not None else None
                group_proxies = [proxies[index] for index in indices] if proxies is not None else None
                label = name
                if key is not None:
                    rate, (width, height) = key
                    label += (f" {rate:g} fps" if by_rate else "") + (f" {width}x{height}" if by_size else "")
                path = folder / (label + (".drt" if mode == "timeline" else ".drb"))
                if mode == "timeline":
                    stabilised += (
                        write_timeline(
                            path,
                            group,
                            name=label,
                            stabilise=True,
                            rotations=turns,
                            full_resolution=full_resolution,
                            separate_audio=separate_audio,
                            link_audio=link_audio,
                            proxies=group_proxies,
                            progress=progress,
                        )
                        or 0
                    )
                else:
                    if progress is not None:
                        progress(None, "Writing the bin")
                    write_bin(
                        path,
                        group,
                        name=label,
                        rotations=turns,
                        separate_audio=separate_audio,
                        link_audio=link_audio,
                        proxies=group_proxies,
                    )
                result.archives.append(path)
                result.archive = result.archive or path
                paths.update(dict.fromkeys(indices, path))
            if on_archive is not None:
                on_archive()
            if progress is not None:
                progress(None, "Opening it in Resolve")
            for path in result.archives:
                open_archive(path)
                dispatched.add(path)
            result.launched = True
            kind = "timeline" if mode == "timeline" else "bin"
            split = " and ".join(
                part for part, used in (("frame rate", by_rate), ("displayed resolution", by_size)) if used
            )
            message = (
                f"Sent {result.archive} to its desktop file handler. Complete the {kind} import in Resolve."
                if len(groups) == 1
                else f"Sent {len(groups)} timelines, one for each {split}. Complete their imports in Resolve."
            )
            if mode == "timeline" and not full_resolution:
                message += " Timeline dimensions fit within 3840x2160; mounted media retain their full size."
            if stabilised:
                message += (
                    f" {stabilised} of {len(mounts)} clips are stabilised from their gyro logs, with their"
                    " Resolve settings. Changing Resolve's stabilisation"
                    " controls or pressing Stabilize in Resolve may replace them. Regenerate in MCRAWsome"
                    " after edits."
                    " Camera Gyro may be absent from Resolve's mode list for CinemaDNG; send again from"
                    " MCRAWsome to restore the supplied mode and corrections."
                    " Gyro log, settings and plan are saved beside each timeline in its .drt.gyro folder."
                )
        except (McError, OSError, ValueError, RecipeError, ProfileError, subprocess.SubprocessError) as exc:
            _LOG.warning("Native Resolve %s send failed: %s", mode, exc)
            message = str(exc)
            if result.archive is not None:
                message += f". The archive is saved at {result.archive}; the media remains mounted."
    result.results = [
        OutputResult(
            id="mount-" + mount.id,
            message=message,
            archive=paths.get(index),
            dispatched=paths.get(index) in dispatched,
        )
        for index, mount in enumerate(mounts)
    ]
    return result
