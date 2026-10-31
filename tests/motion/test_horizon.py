from dataclasses import replace

import numpy as np
import pytest

from mcrawsome.camera.profile import Axes
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.geometry import quat_to_matrix
from mcrawsome.motion.horizon import GRAVITY, reference_up
from mcrawsome.motion.orientation import AxisMapping, Orientation
from mcrawsome.motion.plan import StabilisationPlan, border_inside, plan_stabilisation

AXES = Axes(axes=(1, 0, 2), signs=(-1, -1, -1))
MAPPING = AxisMapping((0, 1, 2), (1, -1, 1))


def sensors(angle: float = 8, roll_rate: float = 0) -> tuple[GyroLog, Orientation, np.ndarray]:
    t = np.arange(0, 6, 0.005)
    gyro = np.tile([0, 0, roll_rate], (len(t), 1))
    log = GyroLog(t, gyro, None, None, "XYZ")
    orientation = Orientation.from_gyro(log, MAPPING)
    a = np.radians(angle)
    up = np.array([-np.sin(a), -np.cos(a), 0])
    camera = np.einsum("nij,j->ni", quat_to_matrix(orientation.quats), up)
    basis = np.eye(3)[list(AXES.axes)] * np.array(AXES.signs)[:, None]
    return replace(log, accel=GRAVITY * camera @ basis), orientation, up


def plan(
    log: GyroLog,
    orientation: Orientation,
    crop: float = 1.4,
    display_rotation: int = 0,
    recorded_flip: bool = False,
    readout_s: float = 0.018,
) -> tuple[StabilisationPlan, np.ndarray]:
    centres = 0.2 + np.arange(120) / 24
    up = reference_up(log, orientation, AXES, centres)
    result = plan_stabilisation(
        orientation,
        centres,
        readout_s,
        0.5,
        crop,
        1,
        12,
        960,
        540,
        24,
        750,
        horizon_up=up,
        horizon_rotation=display_rotation,
        rotate_180=recorded_flip,
    )
    return result, centres


@pytest.mark.parametrize("rate", [0, 0.03, -0.03])
def test_levels_static_tilt_and_slow_roll_without_changing_the_sensor_log(rate: float) -> None:
    log, orientation, up = sensors(roll_rate=rate)
    assert log.accel is not None
    before = log.accel.copy()
    result, _ = plan(log, orientation, crop=1.5)
    displayed = np.einsum("nij,j->ni", quat_to_matrix(result.target_quats), up)
    np.testing.assert_allclose(displayed[:, 0], 0, atol=1e-6)
    assert np.all(displayed[:, 1] < -0.99)
    np.testing.assert_array_equal(log.accel, before)
    for frame in result.rotations:
        assert border_inside(frame, 750, result.zoom, 960, 540)


@pytest.mark.parametrize(("rotation", "flip"), [(90, False), (180, False), (270, False), (0, True)])
def test_respects_manual_display_orientation_and_recorded_flip(rotation: int, flip: bool) -> None:
    log, orientation, up = sensors(angle=rotation + (180 if flip else 0) + 4)
    result, _ = plan(log, orientation, display_rotation=rotation, recorded_flip=flip)
    a = np.radians(rotation + (180 if flip else 0))
    display = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    shown = np.einsum("ij,njk,k->ni", display, quat_to_matrix(result.target_quats), up)
    np.testing.assert_allclose(shown[:, 0], 0, atol=1e-6)
    assert np.all(shown[:, 1] < -0.99)


def test_crop_limits_lock_instead_of_exposing_black_borders() -> None:
    log, orientation, _ = sensors(angle=25)
    result, _ = plan(log, orientation, crop=1.05)
    assert result.reduced_frames == 120
    assert result.max_correction_deg < 25
    assert all(border_inside(r, 750, result.zoom, 960, 540) for r in result.rotations)


def test_bridges_a_short_acceleration_without_following_it_as_gravity() -> None:
    log, orientation, up = sensors()
    assert log.accel is not None
    log.accel[(log.times_s > 2) & (log.times_s < 2.3)] *= 2
    result, _ = plan(log, orientation)
    np.testing.assert_allclose(
        np.einsum("nij,j->ni", quat_to_matrix(result.target_quats), up)[:, 0], 0, atol=1e-6
    )


def test_gravity_uses_full_physical_gyro_when_the_picture_fit_reduces_roll() -> None:
    log, physical, up = sensors(roll_rate=0.03)
    fitted = Orientation.from_gyro(log, MAPPING, gains=(1, 1, 0.5))
    result, centres = plan(log, fitted, crop=1.5, readout_s=0)
    camera_up = np.einsum("nij,j->ni", quat_to_matrix(physical.at(centres)), up)
    shown = np.einsum("nij,nj->ni", result.rotations[:, 6], camera_up)
    np.testing.assert_allclose(shown[:, 0], 0, atol=1e-6)
    assert np.all(shown[:, 1] < -0.99)


@pytest.mark.parametrize("problem", ["missing", "sustained", "initial", "gap", "sky", "nan", "reflection"])
def test_refuses_untrustworthy_gravity(problem: str) -> None:
    log, orientation, _ = sensors()
    assert log.accel is not None
    accel = log.accel
    axes = AXES
    if problem == "missing":
        log = replace(log, accel=None)
    elif problem == "sustained":
        accel[log.times_s > 2] *= 2
    elif problem == "initial":
        accel[log.times_s < 1.1] *= 2
    elif problem == "gap":
        log = replace(log, times_s=np.r_[log.times_s[:400], log.times_s[400:] + 1])
        orientation = Orientation.from_gyro(log, MAPPING)
    elif problem == "sky":
        accel[:] = [0, 0, -GRAVITY]
    elif problem == "nan":
        accel[50, 0] = np.nan
    else:
        axes = Axes(axes=(0, 1, 2), signs=(1, 1, -1))

    def requested_lock() -> None:
        centres = 0.2 + np.arange(120) / 24
        up = reference_up(log, orientation, axes, centres)
        plan_stabilisation(orientation, centres, 0, 0.5, 1.4, 1, 1, 960, 540, 24, 750, horizon_up=up)

    with pytest.raises(ValueError, match="Horizon Lock"):
        requested_lock()
