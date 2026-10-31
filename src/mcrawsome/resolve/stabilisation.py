"""Frame-global corrections in Resolve 21.1.1's measured native Perspective cache.

The fixed 1280x720 cache space is part of the observed format, not source geometry.
Crop lives only in the reciprocal-zoom scalar; matrices contain rotation alone.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome.motion.geometry import intrinsics
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.resolve.bin_codec import WireField, decode_wire, encode_wire, patch_wire, unwrap, wrap
from mcrawsome.resolve.geometry import matching_aspect

_CACHE_WIDTH, _CACHE_HEIGHT = 1280, 720
_MAX_CACHE_BYTES = 8 << 20  # same bound as the native archive codec
_SEED = Path(__file__).parent / "data/perspective_21_1/version.xml"


def section(raw: bytes, number: int) -> bytes:
    matches = [f.value for f in decode_wire(raw) if f.number == number]
    if len(matches) != 1 or not isinstance(matches[0], bytes):
        raise ValueError("Unsupported native Perspective scaffold")
    return matches[0]


def at_path(raw: bytes, path: tuple[int, ...], change: Callable[[bytes], bytes]) -> bytes:
    if not path:
        return change(raw)
    return patch_wire(raw, {path[0]: at_path(section(raw, path[0]), path[1:], change)})


@dataclass(frozen=True)
class PerspectiveCorrections:
    matrices: npt.NDArray[np.float32]  # output-to-input, cache coordinates, no zoom
    zoom: float
    width: int
    height: int
    orientation: int

    def validate(self, frames: int, width: int, height: int, orientation: int) -> None:
        if (self.width, self.height, self.orientation) != (width, height, orientation):
            raise ValueError("Gyro plan geometry or orientation differs from the mounted media")
        if orientation not in (1, 3):
            raise ValueError("Gyro timeline supports DNG orientations 1 and 3 only")
        if (
            width < 1
            or height < 1
            or frames < 1
            or self.matrices.shape != (frames, 3, 3)
            or not np.isfinite(self.matrices).all()
            or not np.isfinite(self.zoom)
            or not 1 <= self.zoom <= 1.5
        ):
            raise ValueError("Invalid gyro matrices, frame count or crop")
        if np.any(np.abs(np.linalg.det(self.matrices.astype(np.float64))) < 1e-12):
            raise ValueError("Singular gyro correction matrix")
        if not np.allclose(self.matrices[:, 2, 2], 1):
            raise ValueError("Gyro matrices must be normalised")


def from_plan(
    plan: StabilisationPlan,
    source_frames: Sequence[int],
    *,
    orientation: int,
    rotation: int = 0,
    check: Callable[[], None] = lambda: None,
) -> PerspectiveCorrections:
    """Map actual mounted source indices, including repeats or trims, to local cache frames.

    Plans use raw sensor coordinates. Conjugate the forward homography by the
    TIFF orientation and cache scaling, then invert for native output-to-input storage.
    Multi-band plans are refused even when their middle band looks usable.
    """
    if plan.rotations.ndim != 4 or plan.rotations.shape[1:] != (1, 3, 3):
        raise ValueError("Native gyro timelines require a frame-global plan; row bands cannot be discarded")
    if (
        rotation not in (0, 180)
        or orientation not in (1, 3)
        or (plan.rotate_180 != (rotation == 180)) != (orientation == 3)
    ):
        raise ValueError("Gyro plan and DNG orientation disagree")
    if (
        not np.isfinite(plan.focal_px)
        or plan.focal_px <= 0
        or plan.width < 1
        or plan.height < 1
        or not np.isfinite(plan.coverage)
        or not 0.9 <= plan.coverage <= 1
    ):
        raise ValueError("Invalid gyro focal length, geometry or insufficient coverage")
    indices = np.asarray(source_frames)
    if (
        indices.ndim != 1
        or not len(indices)
        or indices.dtype.kind not in "iu"
        or indices.min() < 0
        or indices.max() >= len(plan.rotations)
    ):
        raise ValueError("Gyro source frame mapping is outside the plan")
    flip = np.eye(3)
    if orientation == 3:
        flip = np.array([[-1.0, 0, plan.width - 1], [0, -1.0, plan.height - 1], [0, 0, 1.0]])
    scale = np.diag([_CACHE_WIDTH / plan.width, _CACHE_HEIGHT / plan.height, 1.0])
    basis = scale @ flip
    inverse_basis = np.linalg.inv(basis)
    matrices = np.empty((len(indices), 3, 3), dtype=np.float32)
    k = intrinsics(plan.focal_px, plan.width, plan.height)
    inverse_k = np.linalg.inv(k)
    for start in range(0, len(indices), 256):
        check()
        stop = min(start + 256, len(indices))
        correction = plan.rotations[indices[start:stop], 0].astype(np.float64)
        if not np.isfinite(correction).all() or not np.allclose(
            correction.transpose(0, 2, 1) @ correction, np.eye(3), atol=1e-4
        ):
            raise ValueError("Invalid gyro correction rotation")
        if not np.isclose(np.linalg.det(correction), 1, atol=1e-4).all():
            raise ValueError("Invalid gyro correction rotation")
        forward = basis @ (k @ correction @ inverse_k) @ inverse_basis
        inverse = np.linalg.inv(forward)
        denominator = inverse[:, 2:3, 2:3]
        if not np.isfinite(inverse).all() or np.any(np.abs(denominator) < 1e-12):
            raise ValueError("Invalid gyro projection")
        matrices[start:stop] = inverse / denominator
    result = PerspectiveCorrections(matrices, plan.zoom, plan.width, plan.height, orientation)
    result.validate(len(indices), plan.width, plan.height, orientation)
    return result


def version(
    corrections: PerspectiveCorrections,
    timeline_size: tuple[int, int],
    *,
    display_rotation: int = 0,
    check: Callable[[], None] = lambda: None,
) -> ET.Element:
    """A sanitised local version with native globals and one active 80-byte block per frame."""
    corrections.validate(
        len(corrections.matrices), corrections.width, corrections.height, corrections.orientation
    )
    width, height = timeline_size
    if width < 1 or height < 1 or display_rotation not in (0, 90, 180, 270):
        raise ValueError("Invalid gyro timeline geometry")
    source_width, source_height = corrections.width, corrections.height
    if display_rotation % 180:
        source_width, source_height = source_height, source_width
    if not matching_aspect((source_width, source_height), (width, height)):
        raise ValueError("Gyro timelines currently require matching source and timeline aspect ratios")
    tree = ET.fromstring(_SEED.read_text())
    body = tree.find(".//Body")
    if body is None:
        raise ValueError("Missing native Perspective scaffold")
    raw, mode = unwrap(bytes.fromhex(body.text or ""))
    # Native clip processing geometry: output, input, output. Keep all unknown fields.
    raw = at_path(
        raw,
        (1, 3),
        lambda r: patch_wire(
            r,
            {
                1: width,
                2: height,
                4: corrections.width,
                5: corrections.height,
                7: width,
                8: height,
                9: 1 if height > width else 0xFFFFFFFF,
            },
        ),
    )

    # Clip Attributes turns the input before the Perspective cache is evaluated.
    # Its fixed 1280x720 coordinates must follow that turn. Camera Gyro poses
    # remain in their recorded camera basis and do not need this conversion.
    cw, ch = _CACHE_WIDTH, _CACHE_HEIGHT
    display = (
        np.eye(3),
        np.array([[0, -cw / ch, cw], [ch / cw, 0, 0], [0, 0, 1.0]]),
        np.array([[-1, 0, cw], [0, -1, ch], [0, 0, 1.0]]),
        np.array([[0, cw / ch, 0], [-ch / cw, 0, ch], [0, 0, 1.0]]),
    )[display_rotation // 90]
    inverse_display = np.linalg.inv(display)

    def frames(params: bytes) -> bytes:
        template = section(section(params, 4), 1)
        entries = decode_wire(template)
        output = []
        for index, matrix in enumerate(corrections.matrices):
            check()
            if display_rotation:
                matrix = display @ matrix.astype(np.float64) @ inverse_display
                matrix = matrix / matrix[2, 2]
            frame = [WireField(1, 0, 2 * index)] if index else []  # zigzag int, local source index
            for entry in entries:
                if entry.number != 2 or not isinstance(entry.value, bytes):
                    raise ValueError("Unsupported native Perspective frame scaffold")
                key = next(f.value for f in decode_wire(entry.value) if f.number == 1)
                if key == 0x10300015:
                    block = struct.pack(">5Q", 2, _CACHE_WIDTH, _CACHE_HEIGHT, 0, 0)
                    block += struct.pack("<10f", 1 / corrections.zoom, *matrix.ravel())
                    value = patch_wire(section(entry.value, 2), {5: block})
                    entry = WireField(2, 2, patch_wire(entry.value, {2: value}))
                frame.append(entry)
            output.append(WireField(1, 2, encode_wire(frame)))
        return patch_wire(params, {4: encode_wire(output)})

    raw = at_path(raw, (3, 9, 1, 6, 2), frames)
    if len(raw) > _MAX_CACHE_BYTES:
        raise ValueError("Gyro timeline cache exceeds 8 MiB; export a shorter clip")
    body.text = wrap(raw, mode).hex()  # codec bounds also apply to long clips; never truncate
    return tree
