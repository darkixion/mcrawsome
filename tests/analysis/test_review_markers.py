from dataclasses import replace

import numpy as np
import numpy.typing as npt

from mcrawsome.analysis.review_markers import focus, separate, tilt
from mcrawsome.resolve.markers import Marker, encode


def up(degrees: float, frames: int = 72) -> npt.NDArray[np.float64]:
    angle = np.radians(degrees)
    return np.tile([-np.sin(angle), -np.cos(angle), 0], (frames, 1))


def test_tilt_requires_sustained_calibrated_angle_and_accounts_for_display_rotation() -> None:
    assert tilt(up(4), 24, "Recorded tilt") == []
    assert tilt(up(8, 12), 24, "Recorded tilt") == []
    [recorded] = tilt(up(8), 24, "Recorded tilt")
    assert (recorded.frame, recorded.duration) == (0, 72)
    assert tilt(up(90), 24, "Remaining tilt", 90) == []
    assert tilt(np.tile([0, 0, 1.0], (72, 1)), 24, "Recorded tilt") == []


def test_corrected_and_recorded_tilt_are_separate_and_level_correction_removes_only_remaining() -> None:
    recorded = tilt(up(12), 24, "Recorded tilt")
    assert recorded
    assert tilt(up(0), 24, "Remaining tilt") == []
    both = separate(recorded + tilt(up(7), 24, "Remaining tilt"), 72)
    assert [item.name for item in both] == ["Recorded tilt", "Remaining tilt"]
    assert [item.frame for item in both] == [0, 1]
    assert [item.colour for item in both] == ["Pink", "Cyan"]
    encode(both)


def test_focus_hunting_ignores_smooth_pull_noise_and_missing_metadata() -> None:
    t = np.arange(240) / 24
    assert focus(t / 3, 24) == []
    assert focus(1 + 0.01 * np.sin(t * 10), 24) == []
    assert focus(np.full(240, np.nan), 24) == []
    hunting = focus(1 + 0.5 * np.sin(t * 2 * np.pi), 24)
    assert hunting
    assert all(marker.colour == "Purple" for marker in hunting)


def test_coincident_one_frame_events_preserve_both_notes() -> None:
    first = Marker(0, 1, "Red", "Gap", "Gap note")
    result = separate([first, replace(first, name="Clipping", note="Clipping note")], 1)
    assert len(result) == 1
    assert "Gap note" in result[0].note
    assert "Clipping note" in result[0].note
