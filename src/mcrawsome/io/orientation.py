"""Display turns without changing RAW sensor coordinates or MotionCam screen enums."""


def dng_orientation(recorded: int, rotation: int = 0) -> int:
    """TIFF orientation after a clockwise turn of MCRAWsome's established recorded display.

    MotionCam's screen enum is not TIFF Orientation: only recorded value 3 currently
    means a 180-degree turn in our reader. Keep that convention when no override is set.
    """
    if rotation not in (0, 90, 180, 270):
        raise ValueError("RAW rotation must be 0, 90, 180 or 270 degrees")
    degrees = (180 if recorded == 3 else 0) + rotation
    return (1, 6, 3, 8)[degrees % 360 // 90]
