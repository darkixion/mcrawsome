"""Conservative editor review suggestions, in output-frame time, rather than shot-quality scores."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import numpy.typing as npt

from mcrawsome.resolve.markers import Marker, MarkerColour

Array = npt.NDArray[np.float64]


REVIEW_COLOURS: dict[str, MarkerColour] = {
    "Possible shake": "Red",
    "Frequent focus changes": "Purple",
    "Recorded tilt": "Pink",
    "Remaining tilt": "Cyan",
    "Highlight clipping": "Yellow",
    "Low recorded signal": "Blue",
    "Recording gap": "Sand",
    "Motion blur": "Green",
}


REVIEW_CATEGORIES = {
    "Possible shake": "shake",
    "Frequent focus changes": "focus",
    "Recorded tilt": "recorded_tilt",
    "Remaining tilt": "remaining_tilt",
    "Highlight clipping": "clipping",
    "Low recorded signal": "low_signal",
    "Recording gap": "gaps",
    "Motion blur": "blur",
}


def intervals(
    mask: npt.NDArray[np.bool_], rate: float, name: str, note: str, minimum: float = 1
) -> list[Marker]:
    changes = np.diff(np.r_[False, mask, False].astype(np.int8))
    return [
        Marker(int(first), int(last - first), REVIEW_COLOURS.get(name, "Red"), name, note)
        for first, last in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1), strict=True)
        if last - first >= max(1, round(rate * minimum))
    ]


def tilt(up: Array, rate: float, name: str, turn: int = 0) -> list[Marker]:
    angles = (np.degrees(np.arctan2(-up[:, 0], -up[:, 1])) - turn + 180) % 360 - 180
    valid = np.isfinite(up).all(axis=1) & (np.linalg.norm(up[:, :2], axis=1) >= 0.5)
    mask = np.zeros(len(up), dtype=bool)
    active = False
    for index, angle in enumerate(angles):
        active = bool(valid[index] and abs(angle) > (3 if active else 5))
        mask[index] = active
    return intervals(
        mask,
        rate,
        name,
        (
            "Calibrated gravity suggests tilt above 5 degrees for at least one second (release at "
            "3 degrees). Review composition; intentional angles are valid."
        ),
    )


def focus(values: Array, rate: float) -> list[Marker]:
    mask = np.zeros(len(values), dtype=bool)
    valid = np.isfinite(values) & (values >= 0)
    bounds = np.diff(np.r_[False, valid, False].astype(np.int8))
    for first, last in zip(np.flatnonzero(bounds == 1), np.flatnonzero(bounds == -1), strict=True):
        if last - first < rate:
            continue
        window = max(1, round(rate * 0.12)) | 1
        from scipy.ndimage import median_filter  # scipy loads only when needed
        from scipy.signal import find_peaks

        smooth = median_filter(values[first:last], size=window, mode="nearest")
        high = find_peaks(smooth, prominence=0.15)[0]
        low = find_peaks(-smooth, prominence=0.15)[0]
        turns = np.sort(np.r_[high, low]) + first
        for start, end in zip(turns[:-2], turns[2:], strict=True):
            if (end - start) / rate <= 3:
                mask[start : end + 1] = True
    return intervals(
        mask,
        rate,
        "Frequent focus changes",
        (
            "At least three focus reversals of 0.15 dioptres within three seconds. Recorded lens "
            "metadata only; review for hunting or intentional focus pulls."
        ),
        minimum=0.25,
    )


def blur(values: Array, rate: float) -> list[Marker]:
    """Stretches of half a second or more whose shake blur (pixels, from the gyro log) stays high."""
    from mcrawsome.analysis.motion_summary import BLURRED_PX, blurred_stretches

    return [
        Marker(
            first,
            count,
            REVIEW_COLOURS["Motion blur"],
            "Motion blur",
            f"Shake blur of {BLURRED_PX:g} px or more for {count / rate:.1f} s (up to {peak:.0f} px), from "
            "the gyro log and shutter. Stabilisation steadies the picture but cannot remove the blur. A "
            "long shutter or fast movement can be intended.",
        )
        for first, count, peak in blurred_stretches(values, rate)
    ]


def separate(markers: list[Marker], frames: int) -> list[Marker]:
    """Resolve allows one timeline marker per frame. Keep overlapping warnings as distinct markers."""
    used: dict[int, Marker] = {}
    for marker in sorted(markers, key=lambda item: (item.frame, item.name)):
        end = min(frames, marker.frame + marker.duration)
        position = next((index for index in range(marker.frame, end) if index not in used), None)
        if position is None:
            # A one-frame event can coincide with another one-frame event. Preserve both messages.
            previous = used.get(marker.frame)
            if previous is not None:
                used[marker.frame] = replace(previous, note=f"{previous.note}\n{marker.name}: {marker.note}")
            continue
        used[position] = replace(
            marker,
            frame=position,
            duration=end - position,
            note=marker.note
            + (f" Original warning starts at clip frame {marker.frame}." if position != marker.frame else ""),
        )
    return sorted(used.values(), key=lambda item: item.frame)
