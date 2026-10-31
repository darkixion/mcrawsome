"""Raw development: MotionCam Bayer frame to scene-linear ACEScg (AP1)."""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome import _raw
from mcrawsome.camera.colour import camera_to_ap1
from mcrawsome.camera.shading import ShadingMode, shading_gains
from mcrawsome.develop.demosaic import Layout, demosaic
from mcrawsome.develop.highlights import recover_highlights
from mcrawsome.io.metadata import ContainerMetadata, FrameMetadata

Image = npt.NDArray[np.float32]
Plane = npt.NDArray[np.float32]
Demosaic = Callable[[Image, Layout], Image]
_SITES = ((0, 0), (0, 1), (1, 0), (1, 1))
_CHANNELS = {"r": 0, "g": 1, "b": 2}
_CLIP_MARGIN = 2.0


def median_neutral(neutrals: Sequence[tuple[float, float, float]]) -> tuple[float, float, float]:
    m = np.median(np.asarray(neutrals, dtype=np.float64), axis=0)
    return float(m[0]), float(m[1]), float(m[2])


def _to_neutral(rgb: Image, clipped: npt.NDArray[np.uint8], half: bool = False) -> Image:
    """Blend softly towards neutral (the brightest channel, at least sensor white) around `clipped`. With
    `half`, the frame has one pixel per 2x2 quad and the blend covers the same area of the picture."""
    size, sigma = (3, 1.5) if half else (5, 3.0)
    mask = cv2.GaussianBlur(
        cv2.dilate(clipped, np.ones((size, size), np.uint8)).astype(np.float32), (0, 0), sigma
    )
    brightest = np.maximum(np.maximum(rgb[..., 0], rgb[..., 1]), rgb[..., 2])  # faster than max(axis=2)
    level = np.maximum(brightest, np.float32(1.0))[..., None]
    return np.asarray(rgb * (1 - mask[..., None]) + level * mask[..., None], dtype=np.float32)


Levels = tuple[float, float, float, float]


def levels(frame: FrameMetadata, black: float | Levels | None, white: float | None) -> tuple[Levels, float]:
    """The frame's black levels (per Bayer site) and white level, with the Recipe's overrides in place."""
    if black is None:
        blacks = frame.black_level
    elif isinstance(black, tuple):
        blacks = black
    else:
        blacks = (float(black),) * 4
    b0, b1, b2, b3 = (float(b) for b in blacks)
    return (b0, b1, b2, b3), float(frame.white_level if white is None else white)


class Developer:
    """Develops every frame of a clip with one fixed white balance (no flicker)."""

    def __init__(
        self,
        container: ContainerMetadata,
        first_frame: FrameMetadata,
        neutral: tuple[float, float, float],
        highlight_recovery: float = 1.0,
        lens_shading: float = 1.0,
        *,
        shading_mode: ShadingMode = "full",
        black_level: float | Levels | None = None,
        white_level: float | None = None,
        gains: tuple[Plane, Plane, Plane, Plane] | None = None,
        demosaic: Demosaic = demosaic,
    ) -> None:
        """`black_level` and `white_level` replace the clip's levels when set. `gains` replaces the frame's
        lens shading map (R, G on R rows, G on B rows, B, at half size; a still's GainMap opcodes or a flat
        field); `demosaic` replaces Malvar-He-Cutler."""
        self.cfa = container.sensor_arrangement
        self.recorded_blacks = first_frame.black_level
        self.recorded_white = first_frame.white_level
        self._demosaic = demosaic
        self.highlight_recovery = highlight_recovery
        blacks, self.white = levels(first_frame, black_level, white_level)
        self.matrix, self.cct = camera_to_ap1(container, neutral)
        self.rotate_180 = first_frame.orientation == 3
        planes = shading_gains(first_frame, shading_mode) if gains is None else gains
        shading = tuple(plane**lens_shading for plane in planes)  # 0 keeps the vignette
        wb = 1.0 / np.asarray(neutral, dtype=np.float32)
        h, w = first_frame.height, first_frame.width
        r_row = _SITES[self.cfa.index("r")][0]
        self.gain = np.empty((h, w), np.float32)
        self.black = np.empty((h, w), np.float32)
        for i, (y, x) in enumerate(_SITES):
            colour = self.cfa[i]
            black = blacks[i]
            if colour == "r":
                plane, balance = shading[0], wb[0]
            elif colour == "b":
                plane, balance = shading[3], wb[2]
            else:
                plane, balance = (shading[1] if y == r_row else shading[2]), wb[1]
            self.gain[y::2, x::2] = plane * balance / (self.white - black)
            self.black[y::2, x::2] = black
        self._half: list[tuple[int, int, int, float, Image]] | None = None  # develop_half's planes
        self._active_levels = (blacks, self.white)
        self._level_reference = (self.black, self.gain, self.white, blacks)

    def with_levels(self, frame: FrameMetadata) -> Developer:
        """Reuse colour/shading calibration with this frame's recorded levels.

        Return a new developer only when levels change, so device calibration caches see new arrays.
        Legacy recipes keep their historical first-frame preview calibration.
        """
        blacks, white = levels(frame, None, None)
        if (blacks, white) == self._active_levels:
            return self
        if white <= max(blacks):
            raise ValueError("recorded white level must be above every black level")
        updated = copy.copy(self)
        base_black, base_gain, base_white, base_levels = self._level_reference
        updated.black = base_black.copy()
        updated.gain = base_gain.copy()
        for i, (y, x) in enumerate(_SITES):
            updated.gain[y::2, x::2] *= (base_white - base_levels[i]) / (white - blacks[i])
            updated.black[y::2, x::2] = blacks[i]
        updated.white = white
        updated._active_levels = (blacks, white)
        updated.recorded_blacks, updated.recorded_white = frame.black_level, frame.white_level
        updated._half = None
        return updated

    def develop(self, raw: npt.NDArray[np.uint16]) -> Image:
        return self.to_ap1(self.camera_rgb(raw))

    def camera_rgb(self, raw: npt.NDArray[np.uint16], *, highlights: bool = True) -> Image:
        """White-balanced camera RGB (1.0 at sensor white), demosaiced. Measurements can skip highlight
        handling and mask clipped sensor samples themselves."""
        mosaic = (raw.astype(np.float32) - self.black) * self.gain
        rgb = np.maximum(self._demosaic(mosaic, self.cfa), 0.0)
        clipped = (raw >= self.white - _CLIP_MARGIN).astype(np.uint8)
        if highlights and clipped.any():
            rgb = self._highlights(rgb, clipped, lambda: self._channel_masks(clipped), half=False)
        return rgb

    def develop_half(self, raw: npt.NDArray[np.uint16]) -> Image:
        """A quarter-size develop for previews: each 2x2 quad becomes one pixel (R, the mean of the two
        G, B), with the same highlight handling, matrix and turn as `develop`. A quad's channel counts
        as clipped when any of its photosites of that colour is."""
        if self._half is None:
            self._half = [
                (
                    y,
                    x,
                    _CHANNELS[self.cfa[i]],
                    float(self.black[y, x]),
                    np.ascontiguousarray(self.gain[y::2, x::2]),
                )
                for i, (y, x) in enumerate(_SITES)
            ]
        h, w = raw.shape[0] // 2, raw.shape[1] // 2
        planes = [np.zeros((h, w), np.float32) for _ in range(3)]
        clipped = [np.zeros((h, w), np.bool_) for _ in range(3)]
        limit = self.white - _CLIP_MARGIN
        for y, x, channel, black, gain in self._half:
            site = raw[y : 2 * h : 2, x : 2 * w : 2]
            value = (site.astype(np.float32) - np.float32(black)) * gain
            if channel == 1:
                planes[1] += np.float32(0.5) * value
            else:
                planes[channel] = value
            clipped[channel] |= site >= limit
        rgb = np.maximum(cv2.merge(planes), np.float32(0.0))
        any_site = clipped[0] | clipped[1] | clipped[2]
        if any_site.any():
            rgb = self._highlights(rgb, any_site.astype(np.uint8), lambda: np.stack(clipped, -1), half=True)
        return self.to_ap1(rgb)

    def develop_fast(self, raw: npt.NDArray[np.uint16], step: int = 2, width: int = 65536) -> Image:
        """Reduced Bayer quads with shading, white balance, highlight recovery and colour."""
        rgb, masks = _raw.develop_preview(
            raw,
            self.black,
            self.gain,
            self.cfa,
            step,
            self.rotate_180,
            width,
            self.white,
            self.highlight_recovery >= 1,
        )
        rgb = self.recover_preview(rgb, masks, recovered=self.highlight_recovery >= 1)
        return np.asarray(cv2.transform(rgb, self.matrix), dtype=np.float32)

    def recover_preview(self, rgb: Image, masks: npt.NDArray[np.uint8], *, recovered: bool = False) -> Image:
        """The shared reduced highlight stage, in white-balanced camera RGB."""
        if self.highlight_recovery >= 1 and not recovered:
            _raw.recover_preview(rgb, masks)
        if self.highlight_recovery >= 1:
            nothing_left = masks[..., 0] & masks[..., 1] & masks[..., 2]
            if nothing_left.any():
                mask = cv2.GaussianBlur(
                    cv2.dilate(nothing_left, np.ones((3, 3), np.uint8)).astype(np.float32), (0, 0), 1.5
                )
                _raw.neutral_preview(rgb, np.asarray(mask, dtype=np.float32))
        else:
            clipped = masks[..., 0] | masks[..., 1] | masks[..., 2]
            if clipped.any():
                rgb = self._highlights(rgb, clipped, lambda: masks.astype(bool), half=True)
        return rgb

    def _highlights(
        self,
        rgb: Image,
        clipped: npt.NDArray[np.uint8],
        channels: Callable[[], npt.NDArray[np.bool_]],
        half: bool,
    ) -> Image:
        """`clipped` marks pixels near any clipped photosite; `channels` gives (h, w, 3) clipped masks."""
        strength = self.highlight_recovery
        # Colour is unreliable near clipped photosites: blend softly towards neutral at or above
        # sensor white (Milestone 3, and strength 0).
        off = _to_neutral(rgb, clipped, half) if strength < 1 else rgb
        if strength <= 0:
            return off
        # Rebuild channels that survive in part; only where nothing survives blend to neutral.
        # A partial strength mixes the two results, so it always lies between them.
        masks = channels()
        nothing_left = masks[..., 0] & masks[..., 1] & masks[..., 2]
        full = _to_neutral(recover_highlights(rgb, masks, 1.0), nothing_left.astype(np.uint8), half)
        return full if strength >= 1 else np.asarray((1 - strength) * off + strength * full, np.float32)

    def to_ap1(self, rgb: Image) -> Image:
        """Camera RGB to scene-linear AP1, turned upright."""
        out: Image = np.asarray(cv2.transform(rgb, self.matrix), dtype=np.float32)
        if self.rotate_180:
            out = np.ascontiguousarray(out[::-1, ::-1])
        return out

    def _channel_masks(self, clipped_sites: npt.NDArray[np.uint8]) -> npt.NDArray[np.bool_]:
        """(h, w, 3) clipped masks per colour channel, from the clipped photosites, dilated so
        demosaicing's spill around them is covered."""
        planes = np.zeros((*clipped_sites.shape, 3), np.uint8)
        for i, (y, x) in enumerate(_SITES):
            channel = _CHANNELS[self.cfa[i]]
            planes[y::2, x::2, channel] = clipped_sites[y::2, x::2]
        kernel = np.ones((3, 3), np.uint8)
        return np.stack([cv2.dilate(planes[..., c], kernel) for c in range(3)], -1).astype(bool)
