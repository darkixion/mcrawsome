"""Resolve 21.1.1 cached Camera Gyro poses and shared pinhole projection.

This consumes MCRAWsome's synchronised plan, never raw gyro samples. A frame-global plan repeats one pose
in the twelve camera-row slots. With `rows`, a twelve-band plan puts each band's pose in its own slot for
rolling-shutter correction. A probe render (scripts/resolve_row_probe.py, 21 October 2026) showed slot k at
stored sensor row k / 11 of the height, first row first, with Resolve interpolating linearly between
slots; the planner samples its bands there (edge_rows).
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.resolve.bin_codec import WireField, decode_wire, encode_wire, integer, patch_wire, unwrap, wrap
from mcrawsome.resolve.geometry import matching_aspect
from mcrawsome.resolve.stabilisation import at_path, section
from mcrawsome.resolve.stabilisation import from_plan as perspective_from_plan

_SEED = Path(__file__).parent / "data/camera_gyro_21_1/version.xml"
_MAX_CACHE_BYTES = 8 << 20
_PROJECTION = 0xC00000CD
_PREFIX = bytes.fromhex("01000c000c00")
ROWS = 12  # camera-row poses per frame in the native cache


@dataclass(frozen=True)
class CameraGyroCorrections:
    quaternions: npt.NDArray[np.float32]  # camera poses, native (x,y,z,w): (frames, 4), or (frames, ROWS, 4)
    queue_times_ns: npt.NDArray[np.float32]  # relative local timeline exposure-centre times
    focal_px: float
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
            not 1 <= width <= 65535
            or not 1 <= height <= 65535
            or frames < 1
            or self.quaternions.shape not in ((frames, 4), (frames, ROWS, 4))
            or self.queue_times_ns.shape != (frames,)
            or not np.isfinite(self.quaternions).all()
            or not np.allclose(np.linalg.norm(self.quaternions, axis=-1), 1, atol=1e-6)
            or not np.isfinite(self.queue_times_ns).all()
            or np.any(self.queue_times_ns < 0)
            or np.any(np.diff(self.queue_times_ns.astype(np.float64)) <= 0)
        ):
            raise ValueError("Invalid Camera Gyro poses, frame count, geometry or queue timing")
        if (
            not np.isfinite(self.focal_px)
            or not 0 < self.focal_px <= np.finfo(np.float32).max
            or not np.isfinite(self.zoom)
            or not 1 <= self.zoom <= 1.5
        ):
            raise ValueError("Invalid gyro focal length or crop")


def from_plan(
    plan: StabilisationPlan,
    source_frames: Sequence[int],
    *,
    orientation: int,
    fps: float,
    centre_offset_s: float,
    rotation: int = 0,
    rows: bool = False,
    check: Callable[[], None] = lambda: None,
) -> CameraGyroCorrections:
    """Convert raw rotations directly; Perspective remains the internal geometry reference. With `rows`,
    a ROWS-band plan gives each camera-row slot its band's pose (see the module's caveat).

    Native rendering already applies the mounted TIFF orientation: inverse(camera)
    for TIFF 1, D inverse(camera) D for TIFF 3. Both therefore need the inverse
    raw correction, without an additional orientation conjugation. Additional
    The independent Clip Attributes display turn is authored by the media writer.
    The queue follows local output time, including repeated/dropped source frames;
    gyro integration and exposure/readout timing have already been applied upstream.
    """
    from scipy.spatial.transform import Rotation  # scipy loads only when a timeline needs it

    check()
    if not np.isfinite(fps) or not 1 <= fps <= 60 or not np.isfinite(centre_offset_s) or centre_offset_s < 0:
        raise ValueError("Invalid Camera Gyro frame rate or exposure-centre offset")
    # Reuse all existing frame-global, source-index, orientation, crop, coverage
    # and proper-rotation validation. Do not derive quaternions from projected matrices.
    if rows and plan.rotations.shape[1:] != (ROWS, 3, 3):
        raise ValueError(f"Rolling-shutter rows need a {ROWS}-band plan")
    # The middle band stands for the frame in the shared frame-global validation.
    checked = replace(plan, rotations=plan.rotations[:, ROWS // 2 : ROWS // 2 + 1]) if rows else plan
    reference = perspective_from_plan(
        checked, source_frames, orientation=orientation, rotation=rotation, check=check
    )
    bands = ROWS if rows else 1
    poses = np.empty((len(source_frames), bands, 4), dtype=np.float32)
    for start in range(0, len(source_frames), 256):
        check()
        stop = min(start + 256, len(source_frames))
        rotations = plan.rotations[np.asarray(source_frames[start:stop]), :bands].astype(np.float64)
        poses[start:stop] = (
            Rotation.from_matrix(rotations.reshape(-1, 3, 3)).inv().as_quat().reshape(-1, bands, 4)
        )
    times = (np.arange(len(source_frames), dtype=np.float64) / fps + centre_offset_s) * 1e9
    if not np.isfinite(times).all() or np.any(times > np.finfo(np.float32).max):
        raise ValueError("Invalid Camera Gyro queue timing")
    result = CameraGyroCorrections(
        poses if rows else poses[:, 0],
        times.astype(np.float32),
        plan.focal_px,
        reference.zoom,
        reference.width,
        reference.height,
        reference.orientation,
    )
    result.validate(len(source_frames), plan.width, plan.height, orientation)
    return result


def _projection(shared: bytes, corrections: CameraGyroCorrections) -> bytes:
    fields = decode_wire(shared)
    count = 0
    for index, entry in enumerate(fields):
        if entry.number != 1 or not isinstance(entry.value, bytes):
            continue
        key = next(f.value for f in decode_wire(entry.value) if f.number == 1)
        if key != _PROJECTION:
            continue
        value = section(entry.value, 2)
        native = section(value, 5)
        if len(native) != 18 or native[:6] != _PREFIX:
            raise ValueError("Unsupported native Camera Gyro projection scaffold")
        block = _PREFIX + struct.pack(
            "<HHff", corrections.width, corrections.height, corrections.focal_px, corrections.zoom
        )
        fields[index] = WireField(1, 2, patch_wire(entry.value, {2: patch_wire(value, {5: block})}))
        count += 1
    if count != 1:
        raise ValueError("Missing native Camera Gyro projection scaffold")
    return encode_wire(fields)


def _bytes_patch(data: bytes, path: tuple[int, ...]) -> Callable[[bytes], bytes]:
    """Prepare a nested byte-field replacement, retaining every unchanged native field."""
    if not path:
        return lambda value: value
    fields = decode_wire(data)
    positions = [index for index, field in enumerate(fields) if field.number == path[0]]
    if len(positions) != 1:
        raise ValueError("Unsupported native Camera Gyro frame scaffold")
    index = positions[0]
    field = fields[index]
    if field.kind != 2 or not isinstance(field.value, bytes):
        raise ValueError("Unsupported native Camera Gyro frame scaffold")
    prefix = encode_wire(fields[:index]) + integer(field.number * 8 + field.kind)
    suffix = encode_wire(fields[index + 1 :])
    child = _bytes_patch(field.value, path[1:])

    def patch(value: bytes) -> bytes:
        value = child(value)
        return prefix + integer(len(value)) + value + suffix

    return patch


def version(
    corrections: CameraGyroCorrections,
    timeline_size: tuple[int, int],
    *,
    display_rotation: int = 0,
    check: Callable[[], None] = lambda: None,
) -> ET.Element:
    """Complete native Camera Gyro version, including shared lens info outside the pose set."""
    corrections.validate(
        len(corrections.quaternions), corrections.width, corrections.height, corrections.orientation
    )
    width, height = timeline_size
    if width < 1 or height < 1 or display_rotation not in (0, 90, 180, 270):
        raise ValueError("Invalid gyro timeline geometry")
    source_width, source_height = corrections.width, corrections.height
    if display_rotation % 180:
        source_width, source_height = source_height, source_width
    if not matching_aspect((source_width, source_height), (width, height)):
        raise ValueError("Gyro timelines currently require matching source and timeline aspect ratios")
    check()
    tree = ET.fromstring(_SEED.read_text())
    body = tree.find(".//Body")
    if body is None:
        raise ValueError("Missing native Camera Gyro scaffold")
    raw, mode = unwrap(bytes.fromhex(body.text or ""))
    raw = at_path(
        raw,
        (1, 3),
        lambda b: patch_wire(
            b,
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
    raw = at_path(raw, (3, 10), lambda b: _projection(b, corrections))

    def frames(params: bytes) -> bytes:
        entries = decode_wire(section(section(params, 4), 1))
        templates: list[tuple[int, bytes | Callable[[bytes], bytes]]] = []
        for entry in entries:
            if entry.number != 2 or not isinstance(entry.value, bytes):
                raise ValueError("Unsupported native Camera Gyro frame scaffold")
            key = next(f.value for f in decode_wire(entry.value) if f.number == 1)
            if key == 0x1030001E:
                templates.append((key, _bytes_patch(entry.value, (2, 5))))
            elif key == 0x1030001F:
                templates.append((key, _bytes_patch(entry.value, (2, 12, 1))))
            elif key == 0x10300017:
                templates.append((key, entry.value))
            else:
                raise ValueError("Unsupported native Camera Gyro parameter")
        if {key for key, _ in templates} != {0x10300017, 0x1030001E, 0x1030001F}:
            raise ValueError("Incomplete native Camera Gyro frame scaffold")
        output = []
        for index, (camera, time_ns) in enumerate(
            zip(corrections.quaternions, corrections.queue_times_ns, strict=True)
        ):
            check()
            frame = [WireField(1, 0, 2 * index)] if index else []
            for key, patch in templates:
                if isinstance(patch, bytes):
                    value = patch
                elif key == 0x1030001E:
                    slots = camera if camera.ndim == 2 else np.broadcast_to(camera, (ROWS, 4))
                    block = struct.pack("<4f", 0, 0, 0, 1) + slots.astype("<f4", copy=False).tobytes()
                    value = patch(block)
                else:
                    value = patch(struct.pack("<5f", time_ns, 0, 0, 0, 1))
                frame.append(WireField(2, 2, value))
            output.append(WireField(1, 2, encode_wire(frame)))
        return patch_wire(params, {4: encode_wire(output)})

    raw = at_path(raw, (3, 9, 1, 6, 2), frames)
    if len(raw) > _MAX_CACHE_BYTES:
        raise ValueError("Gyro timeline cache exceeds 8 MiB; export a shorter clip")
    body.text = wrap(raw, mode).hex()
    return tree
