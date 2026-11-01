from dataclasses import replace

import numpy as np
import pytest

from mcrawsome.camera.profile import Axes
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.geometry import quat_conjugate, quat_mul, quat_to_matrix
from mcrawsome.motion.horizon import GRAVITY, level, level_share, reference_up
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


@pytest.mark.parametrize("problem", ["missing", "sustained", "initial", "gap", "nan", "reflection"])
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


TIP_START, TIP_RATE = 2.5, np.radians(40)  # level for 2.5 seconds, then tipping at 40 degrees a second
STRAIGHT_DOWN = TIP_START + np.radians(90) / TIP_RATE + 1.5  # all of the smoothing window is there by now


def tipping(
    pitch: float, roll: float = 8, turn: int = 1, back: bool = False
) -> tuple[GyroLog, Orientation, np.ndarray]:
    """A camera rolled by `roll` degrees that tips from level to `pitch` degrees up or down (`turn`), about
    the axis level in the world, and holds there or comes back: gyro and accelerometer alike."""
    t = np.arange(0, 8, 0.0025)
    a = np.radians(roll)
    axis = np.array([-np.sin(a), np.cos(a), 0.0])  # the gyro's own axes: x and y swap to the camera's
    rate = np.zeros((len(t), 3))
    tipped = TIP_START + np.radians(pitch) / TIP_RATE
    rate[(t >= TIP_START) & (t < tipped)] = turn * TIP_RATE * axis
    if back:
        rate[(t >= 5.5) & (t < 5.5 + tipped - TIP_START)] = -turn * TIP_RATE * axis
    log = GyroLog(t, rate, None, None, "XYZ")
    orientation = Orientation.from_gyro(log, MAPPING)
    up = np.array([-np.sin(a), -np.cos(a), 0])
    camera = np.einsum("nij,j->ni", quat_to_matrix(orientation.quats), up)
    basis = np.eye(3)[list(AXES.axes)] * np.array(AXES.signs)[:, None]
    return replace(log, accel=GRAVITY * camera @ basis), orientation, up


CENTRES = 0.2 + np.arange(180) / 24


def steps_deg(quats: np.ndarray) -> np.ndarray:
    """How far each orientation turned from the one before it, in degrees."""
    turns = quat_mul(quats[1:], quat_conjugate(quats[:-1]))
    return np.degrees(2 * np.arctan2(np.linalg.norm(turns[:, 1:], axis=1), np.abs(turns[:, 0])))


def tipped_plan(
    log: GyroLog, orientation: Orientation, crop: float = 1.5, rotation: int = 0, flip: bool = False
) -> StabilisationPlan:
    return plan_stabilisation(
        orientation, CENTRES, 0.018, 0.5, crop, 1, 12, 960, 540, 24, 750,
        horizon_up=reference_up(log, orientation, AXES, CENTRES), horizon_rotation=rotation, rotate_180=flip,
    )  # fmt: skip


@pytest.mark.parametrize("turn", [1, -1])
@pytest.mark.parametrize("pitch", [65, 80, 90])
def test_a_camera_tipped_straight_up_or_down_is_still_stabilised(pitch: int, turn: int) -> None:
    log, orientation, _ = tipping(pitch, turn=turn)
    result = tipped_plan(log, orientation)
    assert np.isfinite(result.rotations).all()
    assert all(border_inside(frame, 750, result.zoom, 960, 540) for frame in result.rotations)


def test_a_camera_pointing_straight_up_throughout_is_stabilised_without_levelling() -> None:
    log, orientation, _ = sensors(roll_rate=0.03)
    assert log.accel is not None
    log.accel[:] = [0, 0, -GRAVITY]
    centres = 0.2 + np.arange(120) / 24
    up = reference_up(log, orientation, AXES, centres)
    result = plan_stabilisation(orientation, centres, 0, 0.5, 1.4, 1, 1, 960, 540, 24, 750, horizon_up=up)
    free = plan_stabilisation(orientation, centres, 0, 0.5, 1.4, 1, 1, 960, 540, 24, 750)
    assert np.all(level_share(result.target_quats, up) == 0)
    np.testing.assert_array_equal(result.target_quats, free.target_quats)


@pytest.mark.parametrize("turn", [1, -1])
def test_levels_the_frames_with_a_horizon_and_leaves_the_rest_with_the_cameras_own_roll(turn: int) -> None:
    log, orientation, up = tipping(90, turn=turn)
    result = tipped_plan(log, orientation, crop=8)  # room to spare: only the levelling is on trial
    free = plan_stabilisation(orientation, CENTRES, 0.018, 0.5, 8, 1, 12, 960, 540, 24, 750)
    camera = quat_to_matrix(result.target_quats)
    shown = np.einsum("nij,j->ni", camera, up)
    before, down = CENTRES < TIP_START - 0.5, CENTRES > STRAIGHT_DOWN
    assert before.any()
    assert down.any()
    np.testing.assert_allclose(shown[before, 0], 0, atol=1e-6)  # level, against the camera's 8 degrees
    assert np.all(shown[before, 1] < -0.99)
    assert np.all(level_share(result.target_quats, reference_up(log, orientation, AXES, CENTRES))[down] == 0)
    # Nothing left to level: exactly where the stabilisation without the lock would point.
    matching = np.abs((result.target_quats[down] * free.target_quats[down]).sum(1))
    np.testing.assert_allclose(matching, 1, atol=1e-9)


@pytest.mark.parametrize("back", [False, True])
def test_the_picture_never_jumps_on_the_way_into_or_out_of_the_lock(back: bool) -> None:
    roll = 20  # large, so a jump of the levelling would stand out from the tipping
    log, orientation, _ = tipping(85, roll=roll, back=back)
    result = tipped_plan(log, orientation, crop=8)
    free = plan_stabilisation(orientation, CENTRES, 0.018, 0.5, 8, 1, 12, 960, 540, 24, 750)
    assert steps_deg(result.target_quats).max() < steps_deg(free.target_quats).max() + roll / 4


def test_the_share_of_levelling_falls_smoothly_as_gravity_leaves_the_picture() -> None:
    pitch = np.radians(np.linspace(0, 90, 901))
    a = np.radians(10)
    up = np.stack([-np.sin(a) * np.cos(pitch), -np.cos(a) * np.cos(pitch), -np.sin(pitch)], -1)
    targets = np.tile([1.0, 0, 0, 0], (len(pitch), 1))
    share = level_share(targets, up)
    assert np.all(share[np.cos(pitch) > 0.7 + 1e-9] == 1)  # within 45 degrees of level
    assert np.all(share[np.cos(pitch) < 0.5 - 1e-9] == 0)  # within 30 degrees of straight down
    assert np.all(np.diff(share) <= 0)
    assert np.abs(np.diff(share)).max() < 0.02  # a tenth of a degree of pitch: no step anywhere
    assert steps_deg(level(targets, up)).max() < 0.2


def test_a_camera_that_keeps_its_horizon_is_levelled_in_full() -> None:
    pitch = np.radians(np.linspace(0, 45, 91))
    a = np.radians(30)
    up = np.stack([-np.sin(a) * np.cos(pitch), -np.cos(a) * np.cos(pitch), -np.sin(pitch)], -1)
    targets = np.tile([1.0, 0, 0, 0], (len(pitch), 1))
    turned = level(targets, up)
    np.testing.assert_array_equal(turned[:, [1, 2]], 0)
    expected = np.arctan2(-up[:, 0], -up[:, 1])
    np.testing.assert_allclose(turned[:, 0], np.cos(expected / 2), atol=1e-12)
    np.testing.assert_allclose(turned[:, 3], np.sin(expected / 2), atol=1e-12)


@pytest.mark.parametrize(("rotation", "flip"), [(90, False), (180, False), (270, False), (0, True)])
def test_a_turned_display_keeps_the_cameras_own_roll_too(rotation: int, flip: bool) -> None:
    log, orientation, up = tipping(90, roll=rotation + (180 if flip else 0) + 8)
    result = tipped_plan(log, orientation, crop=8, rotation=rotation, flip=flip)
    free = plan_stabilisation(orientation, CENTRES, 0.018, 0.5, 8, 1, 12, 960, 540, 24, 750)
    down = CENTRES > STRAIGHT_DOWN
    matching = np.abs((result.target_quats[down] * free.target_quats[down]).sum(1))
    np.testing.assert_allclose(matching, 1, atol=1e-9)
    a = np.radians(rotation + (180 if flip else 0))
    display = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    shown = np.einsum("ij,njk,k->ni", display, quat_to_matrix(result.target_quats), up)
    before = CENTRES < TIP_START - 0.5
    np.testing.assert_allclose(shown[before, 0], 0, atol=1e-6)
