"""Bounded idle preparation, favouring the next playback position over random video seeks."""

import math


def warm_frames(
    index: int,
    start: int,
    count: int,
    rate: float,
    width: int,
    size: tuple[int, int],
    cache_mib: int,
    *,
    raw: bool,
) -> list[tuple[int, int]]:
    if cache_mib <= 0 or count <= 0:
        return []
    # Keep the first prepared pictures resident, including linear pixels for native playback.
    # RAW also keeps neighbouring scrub sizes; reserve room for those and the displayed frame.
    frame_bytes = max(1, size[0] * size[1] * 12)
    slots = cache_mib * 1024**2 * 3 // 4 // frame_bytes
    ahead = min(120, max(1, math.ceil(rate * 2)), max(0, slots - (18 if raw else 2)))
    start = max(0, min(start, count - 1))
    prepared = [(frame, width) for frame in range(start, min(count, start + ahead))]
    if raw:
        prepared += [
            (frame, target)
            for distance in range(1, 5)
            for frame in (index - distance, index + distance)
            if 0 <= frame < count
            for target in dict.fromkeys((width, min(640, width)))
        ]
    # LOG uses one size, in increasing time order, so decoder lanes keep moving forwards.
    return list(dict.fromkeys(prepared))
