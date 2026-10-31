"""Resolve 21.1 CinemaDNG Clip Attributes rotation, before timeline image sizing.

The native portrait reference stores a clockwise 90-degree turn as Radiometry
field 19 = 180 (signed degrees, zigzag encoded). Edit Transform stays neutral.
Recorded TIFF orientation and gyro poses remain separate and unchanged.
"""

from mcrawsome.resolve.bin_codec import decode_wire, patch_wire


def clip_rotation(radiometry: bytes, rotation: int) -> bytes:
    """Set an extra clockwise source display turn without changing RAW defaults."""
    if rotation not in (0, 90, 180, 270):
        raise ValueError("Expected a clockwise quarter-turn display rotation")
    fields = decode_wire(radiometry)
    values = {f.number: f.value for f in fields if f.kind == 0}
    if values.get(1) != 312 or values.get(2) != 2:
        raise ValueError("Unsupported Resolve CinemaDNG clip rotation descriptor")
    # Nonnegative degree values use 2*value in the native signed integer wire type.
    # patch_wire also refuses missing/duplicate rotation fields.
    return patch_wire(radiometry, {19: 2 * rotation})
