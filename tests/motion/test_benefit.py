import numpy as np
import pytest

from mcrawsome.motion.benefit import shake_removed_by_plan
from mcrawsome.motion.flow import MotionTrack
from mcrawsome.motion.geometry import homography
from mcrawsome.motion.plan import StabilisationPlan

W, H, FOCAL, ZOOM, N = 960, 540, 750.0, 1.1, 120


def _yaw(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def _plan(angles: np.ndarray) -> StabilisationPlan:
    rotations = np.stack([np.repeat(_yaw(a)[None], 4, axis=0) for a in angles]).astype(np.float32)
    return StabilisationPlan(
        rotations=rotations, target_quats=np.zeros((len(angles), 4)), focal_px=FOCAL, zoom=ZOOM,
        width=W, height=H, reduced_frames=0, mean_correction_deg=0.0, max_correction_deg=0.0, coverage=1.0,
    )  # fmt: skip


def _centre_shift(angles: np.ndarray) -> np.ndarray:
    """Where the plan moves the picture's centre, in source pixels, per frame."""
    c = np.array([W / 2, H / 2, 1.0])
    out = []
    for a in angles:
        p = homography(_yaw(a), FOCAL, W, H, ZOOM) @ c
        out.append(p[0] / p[2] - c[0])
    return np.array(out) / ZOOM


def _track(dx: np.ndarray) -> MotionTrack:
    n = len(dx)
    return MotionTrack(np.arange(n), dx, np.zeros(n), np.zeros(n))


@pytest.mark.parametrize("share", [1.0, 0.5])
def test_a_known_correction_is_measured(share: float) -> None:
    rng = np.random.default_rng(3)
    angles = rng.normal(0, 0.004, N)  # frame-to-frame shake the plan cancels
    shift = _centre_shift(angles)
    shake = -np.diff(shift) / share  # the plan cancels `share` of the measured shake
    assert shake_removed_by_plan(_plan(angles), _track(shake)) == pytest.approx(share, abs=0.02)


def test_a_plan_unrelated_to_the_shake_removes_nothing() -> None:
    rng = np.random.default_rng(4)
    angles = rng.normal(0, 0.004, N)
    shake = rng.normal(0, 3.0, N - 1)
    assert shake_removed_by_plan(_plan(angles), _track(shake)) < 0.05
