"""Immutable original/proxy media dependencies for native Resolve archives."""

from __future__ import annotations

from dataclasses import dataclass

from mcrawsome.mount.manager import MountedSequence, MountManager


@dataclass(frozen=True)
class NativeMediaMounts:
    originals: list[MountedSequence]
    proxies: list[MountedSequence | None]
    rotations: list[int] | None

    @property
    def dependencies(self) -> list[MountedSequence]:
        return list({mount.id: mount for mount in [*self.originals, *self.proxies] if mount}.values())


def _recorded(
    manager: MountManager, mount: MountedSequence, size: str, *, linked: bool = False
) -> MountedSequence:
    update: dict[str, object] = {"rotation": 0, "mount_proxy": size}
    if linked:  # a linked pair's proxy matches its original frame for frame
        update["mount_first_frame"] = "proxy"
    raw = mount.raw_settings.model_copy(update=update)
    if raw == mount.raw_settings:
        return mount
    return manager.mount(
        mount.source,
        expected=mount.stamp,
        location=mount.mount_root,
        vignette_profile=mount.vignette_profile,
        colour_matrix=mount.colour_matrix,
        raw_settings=raw,
        frame_range=mount.frame_range,
        quad_bayer_remosaic=mount.quad_bayer_remosaic,
        timecode=mount.timecode,
    )


def validate_proxy_pair(original: MountedSequence, proxy: MountedSequence) -> None:
    if (
        original.raw_settings.mount_proxy != "full"
        or proxy.raw_settings.mount_proxy == "full"
        or original.raw_settings.rotation != 0
        or proxy.raw_settings.rotation != 0
        or original.source != proxy.source
        or original.stamp != proxy.stamp
        or original.frames != proxy.frames
        or original.frame_range != proxy.frame_range
        or original.quad_bayer_remosaic != proxy.quad_bayer_remosaic
        or original.timecode != proxy.timecode
        or original.vignette_profile != proxy.vignette_profile
        or original.colour_matrix != proxy.colour_matrix
        or original.raw_settings.model_copy(update={"mount_proxy": proxy.raw_settings.mount_proxy})
        != proxy.raw_settings
        or original.path == proxy.path
        or original.id == proxy.id
    ):
        raise ValueError("Original and proxy must share the same immutable capture settings and frame range")


def prepare_media_mounts(
    manager: MountManager,
    selected: list[MountedSequence],
    *,
    linked_proxies: bool = False,
) -> NativeMediaMounts:
    """Preserve the selected mount snapshot when creating auxiliary media.

    Native DRB/DRT sends request linked proxies after playback and original-render
    verification. Other callers can keep their reduced-media behaviour.
    Rotation belongs to Clip Attributes, independently of the served DNG dimensions.
    """
    originals, proxies = [], []
    turns: list[int] = [mount.raw_settings.rotation for mount in selected]
    for mount in selected:
        linked = linked_proxies and mount.raw_settings.mount_proxy != "full"
        editing = _recorded(manager, mount, mount.raw_settings.mount_proxy, linked=linked)
        proxy = editing if linked else None
        original = _recorded(manager, mount, "full", linked=True) if proxy else editing
        if proxy is not None:
            validate_proxy_pair(original, proxy)
        originals.append(original)
        proxies.append(proxy)
    return NativeMediaMounts(originals, proxies, turns if any(turns) else None)
