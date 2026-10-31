from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.geometry import quat_to_matrix
from mcrawsome.motion.orientation import AxisMapping, Orientation
from mcrawsome.motion.plan import StabilisationPlan, border_inside, jitter, plan_stabilisation

FPS, W, H, FOCAL = 24.0, 960, 540, 750.0
MAPPING = AxisMapping((0, 1, 2), (1, -1, 1))


def _orientation(gyro_fn, seconds: float = 6.0, start: float = 0.0) -> Orientation:  # type: ignore[no-untyped-def]
    t = np.arange(start, seconds, 1 / 400)
    log = GyroLog(
        times_s=t, gyro=gyro_fn(t), accel=None, start_epoch_s=None, orientation="XYZ", clock="video"
    )
    return Orientation.from_gyro(log, MAPPING)


def _plan(orient: Orientation, crop: float = 1.1, n: int = 120) -> tuple[StabilisationPlan, np.ndarray]:
    centres = 0.5 + np.arange(n) / FPS
    plan = plan_stabilisation(
        orient, centres, readout_s=0.018, smoothing_s=0.5, crop=crop, strength=1.0, bands=12,
        width=W, height=H, fps=FPS, focal_px=FOCAL,
    )  # fmt: skip
    return plan, centres


def test_no_motion_gives_identity() -> None:
    plan, _ = _plan(_orientation(lambda t: np.zeros((len(t), 3))))
    np.testing.assert_allclose(plan.rotations, np.broadcast_to(np.eye(3), plan.rotations.shape), atol=1e-6)
    assert plan.reduced_frames == 0


def shake(t: np.ndarray) -> np.ndarray:
    yaw = 0.15 + 0.25 * np.sin(2 * np.pi * 7.0 * t)
    pitch = 0.2 * np.sin(2 * np.pi * 5.3 * t + 1)
    roll = 0.05 * np.sin(2 * np.pi * 3.1 * t)
    return np.stack([yaw, pitch, roll], -1)


def test_shake_is_removed_and_the_pan_kept() -> None:
    orient = _orientation(shake)
    plan, centres = _plan(orient)
    actual = orient.at(centres)
    stabilised = plan.target_quats
    assert jitter(stabilised, FPS) <= 0.3 * jitter(actual, FPS)

    def pan(q: np.ndarray) -> float:  # yaw between two frames in the middle of the clip
        yaw = np.degrees(np.arcsin(quat_to_matrix(q)[:, 0, 2]))
        return float(yaw[96] - yaw[24])

    assert pan(stabilised) == pytest.approx(pan(actual), rel=0.15)


def test_a_whip_pan_never_shows_a_border() -> None:
    orient = _orientation(lambda t: np.stack([3.0 * ((t > 2.0) & (t < 2.3)), 0 * t, 0 * t], -1))
    plan, _ = _plan(orient, crop=1.1)
    assert plan.reduced_frames > 0
    for k in range(len(plan.rotations)):
        assert border_inside(plan.rotations[k], FOCAL, plan.zoom, W, H)


def test_frames_outside_the_log_hold_the_nearest_correction() -> None:
    orient = _orientation(shake, seconds=4.0, start=0.0)
    plan, centres = _plan(orient, n=120)  # frames run to 5.46 s, the log ends at 4 s
    covered = centres <= 4.0
    assert plan.coverage == pytest.approx(covered.mean(), abs=0.01)
    last = int(np.flatnonzero(covered)[-1])
    np.testing.assert_allclose(
        plan.rotations[last + 1 :], np.broadcast_to(plan.rotations[last], plan.rotations[last + 1 :].shape)
    )


def test_plan_round_trips_through_a_file(tmp_path: Path) -> None:
    plan, _ = _plan(_orientation(shake))
    path = tmp_path / "plan.npz"
    plan.save(path)
    loaded = StabilisationPlan.load(path)
    np.testing.assert_array_equal(loaded.rotations, plan.rotations)
    assert loaded.zoom == plan.zoom


def test_planning_memory_does_not_grow_with_the_square_of_the_length() -> None:
    import tracemalloc

    n = 5000  # about 3.5 minutes; a square table of frame distances would need 200 MB
    orient = _orientation(lambda t: np.zeros((len(t), 3)), seconds=n / FPS + 1.0)
    tracemalloc.start()
    try:
        _plan(orient, n=n)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 100e6, f"peak {peak / 1e6:.0f} MB"


def test_frames_in_a_hole_hold_the_nearest_correction() -> None:
    t = np.arange(0.0, 6.0, 1 / 400)
    t = t[(t < 2.5) | (t > 3.5)]
    log = GyroLog(t, shake(t), None, None, "XYZ", clock="video")
    plan, centres = _plan(Orientation.from_gyro(log, MAPPING), n=120)
    ((start, end),) = log.holes()
    inside = (centres > start) & (centres < end)
    assert plan.coverage == pytest.approx(1 - inside.mean(), abs=0.01)
    first = int(np.flatnonzero(inside)[0])
    np.testing.assert_allclose(plan.rotations[first], plan.rotations[first - 1])


@pytest.mark.parametrize("rate", [6.0, 10.0])
def test_a_violent_whip_pan_never_shows_a_border(rate: float) -> None:
    # at these speeds the rolling-shutter correction alone exceeds the crop (reviewer: 344 deg/s and up)
    orient = _orientation(lambda t: np.stack([rate * ((t > 2.0) & (t < 2.3)), 0 * t, 0 * t], -1))
    plan, _ = _plan(orient, crop=1.1)
    assert plan.reduced_rolling_shutter_frames > 0
    for k in range(len(plan.rotations)):
        assert border_inside(plan.rotations[k], FOCAL, plan.zoom, W, H), k


def sway_and_jitter(t: np.ndarray) -> np.ndarray:
    """A slow 0.4 Hz sway far too big for a 1.10 crop, plus a small fast 7 Hz jitter (rad/s)."""
    sway = 0.15 * 2 * np.pi * 0.4 * np.cos(2 * np.pi * 0.4 * t)
    jitter_rate = 0.004 * 2 * np.pi * 7.0 * np.cos(2 * np.pi * 7.0 * t)
    return np.stack([sway + jitter_rate, 0 * t, 0 * t], -1)


def _amplitude(q: np.ndarray, t: np.ndarray, hz: float) -> float:
    """Amplitude of the `hz` component of the yaw angle, fitted beside a line and the 0.4 Hz sway."""
    yaw = np.arcsin(quat_to_matrix(q)[:, 0, 2])
    waves = [f(2 * np.pi * f_hz * t) for f_hz in (0.4, hz) for f in (np.sin, np.cos)]
    basis = np.stack([np.ones_like(t), t, *waves], -1)
    coef = np.linalg.lstsq(basis, yaw, rcond=None)[0]
    return float(np.hypot(coef[4], coef[5]))


def test_fast_shake_is_still_removed_when_the_crop_runs_short() -> None:
    orient = _orientation(sway_and_jitter, seconds=8.0)
    plan, centres = _plan(orient, crop=1.1, n=150)
    actual = orient.at(centres)
    assert plan.reduced_frames > 0  # the sway does not fit
    assert plan.followed_frames < plan.reduced_frames  # most held-back frames still follow a smooth path
    # the 7 Hz part alone: a generic high-pass would also count the sway's curvature, which is kept
    assert _amplitude(plan.target_quats, centres, 7.0) <= 0.15 * _amplitude(actual, centres, 7.0)
    for k in range(len(plan.rotations)):
        assert border_inside(plan.rotations[k], FOCAL, plan.zoom, W, H), k


def test_followed_frames_round_trip_through_a_file(tmp_path: Path) -> None:
    plan, _ = _plan(_orientation(sway_and_jitter, seconds=8.0), crop=1.1, n=150)
    path = tmp_path / "plan.npz"
    plan.save(path)
    assert StabilisationPlan.load(path).followed_frames == plan.followed_frames


def test_edge_rows_put_the_first_and_last_bands_on_the_first_and_last_rows() -> None:
    """Resolve's Camera Gyro row slots sit at rows k / (slots - 1), first and last row included (measured
    with scripts/resolve_row_probe.py); the developed export's bands are centred in equal strips."""
    rate, readout = 0.6, 0.02
    orient = _orientation(
        lambda t: np.column_stack([np.zeros_like(t), rate * np.ones_like(t), np.zeros_like(t)])
    )
    times = 1.0 + np.arange(30) / 30
    common = (readout, 0.5, 1.3, 1.0, 12, 1920, 1080, 30.0, 1500.0)
    centred = plan_stabilisation(orient, times, *common)
    edges = plan_stabilisation(orient, times, *common, edge_rows=True)

    def spread(plan: StabilisationPlan) -> float:
        """The turn between the first and last band: the rolling-shutter part across the frame."""
        first, last = plan.rotations[15, 0].astype(float), plan.rotations[15, 11].astype(float)
        return float(np.arccos(np.clip((np.trace(first @ last.T) - 1) / 2, -1, 1)))

    assert spread(edges) == pytest.approx(rate * readout, rel=0.02)  # first row to last: the whole readout
    assert spread(centred) == pytest.approx(rate * readout * 11 / 12, rel=0.02)  # strip centres: 11/12 of it


def test_crop_solver_prepares_its_border_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcrawsome.motion.plan as module

    original = module._border
    calls = 0

    def border(width: int, height: int) -> np.ndarray:
        nonlocal calls
        calls += 1
        return original(width, height)

    monkeypatch.setattr(module, "_border", border)
    result, _ = _plan(_orientation(shake), n=24)
    assert len(result.rotations) == 24
    assert calls == 1


def test_unchanged_corrections_and_border_checks_are_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcrawsome.motion.plan as module

    matrices, border = quat_to_matrix, module._border_test
    conversions = checks = 0

    def counted(q: np.ndarray) -> np.ndarray:
        nonlocal conversions
        conversions += 1
        return matrices(q)

    def prepared(
        focal: float,
        zoom: float,
        width: int,
        height: int,
        bands: int,
        edge_rows: bool = False,
    ) -> Callable[[np.ndarray], bool]:
        inside = border(focal, zoom, width, height, bands, edge_rows)

        def check(rotations: np.ndarray) -> bool:
            nonlocal checks
            checks += 1
            return inside(rotations)

        return check

    monkeypatch.setattr(module, "quat_to_matrix", counted)
    monkeypatch.setattr(module, "_border_test", prepared)
    result, _ = _plan(_orientation(lambda t: np.zeros((len(t), 3))), n=24)
    np.testing.assert_allclose(result.rotations, np.broadcast_to(np.eye(3), result.rotations.shape))
    assert conversions == 24
    assert checks == 24


def _mode_plan(
    orient: Orientation, mode: str, crop: float = 1.3, n: int = 120, display_turn: int = 0
) -> tuple[StabilisationPlan, np.ndarray]:
    centres = 0.5 + np.arange(n) / FPS
    plan = plan_stabilisation(
        orient, centres, readout_s=0.018, smoothing_s=0.5, crop=crop, strength=1.0, bands=12,
        width=W, height=H, fps=FPS, focal_px=FOCAL, mode=mode, display_turn=display_turn,  # type: ignore[arg-type]
    )  # fmt: skip
    return plan, centres


def _turn_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The angle between orientations, per row (degrees)."""
    return np.asarray(np.degrees(2 * np.arccos(np.clip(np.abs((a * b).sum(-1)), 0, 1))), dtype=np.float64)


def test_correcting_only_the_rolling_shutter_keeps_each_frames_framing() -> None:
    orient = _orientation(shake)
    plan, centres = _mode_plan(orient, "rolling_shutter")
    assert _turn_deg(plan.target_quats, orient.at(centres)).max() < 1e-4  # the camera's own path
    first, last = plan.rotations[:, 0], plan.rotations[:, -1]
    assert np.abs(first - np.eye(3)).max() > 1e-4  # the rows are still straightened
    assert np.abs(last - np.eye(3)).max() > 1e-4
    assert plan.reduced_frames == 0
    assert plan.followed_frames == 0
    smoothed, _ = _mode_plan(orient, "smooth")
    assert plan.mean_correction_deg < 0.2 * smoothed.mean_correction_deg


def drift_and_shake(t: np.ndarray) -> np.ndarray:
    yaw = 0.02 + 0.1 * np.sin(2 * np.pi * 6.0 * t)  # a slow drift right, with shake
    pitch = 0.08 * np.sin(2 * np.pi * 4.7 * t + 1)
    roll = 0.04 * np.sin(2 * np.pi * 3.1 * t)
    return np.stack([yaw, pitch, roll], -1)


def test_a_virtual_tripod_holds_one_view() -> None:
    plan, _ = _mode_plan(_orientation(drift_and_shake), "tripod")
    assert plan.reduced_frames == 0
    assert _turn_deg(plan.target_quats, plan.target_quats[:1]).max() < 1e-3  # every frame aims the same way
    smooth, _ = _mode_plan(_orientation(drift_and_shake), "smooth")
    assert _turn_deg(smooth.target_quats, smooth.target_quats[:1]).max() > 0.3  # smoothing lets it drift


def test_a_tripod_lets_go_where_the_crop_runs_out() -> None:
    plan, centres = _mode_plan(_orientation(lambda t: np.stack([0.5 + 0 * t, 0 * t, 0 * t], -1)), "tripod")
    assert plan.reduced_frames > 0  # a 140-degree turn cannot be held inside a 1.3 crop
    for k in range(len(centres)):
        assert border_inside(plan.rotations[k], FOCAL, plan.zoom, W, H)


def pan_with_shake(t: np.ndarray) -> np.ndarray:
    yaw = 0.15 + 0.1 * np.sin(2 * np.pi * 6.0 * t)
    pitch = 0.1 * np.sin(2 * np.pi * 4.7 * t + 1)  # the mapping drives tilt with minus the device's y
    roll = 0.05 * np.sin(2 * np.pi * 3.1 * t)
    return np.stack([yaw, pitch, roll], -1)


def _pan_tilt_roll(q: np.ndarray) -> np.ndarray:
    """Small-angle turns about the camera's y (pan), x (tilt) and z (roll), from the rotation matrices."""
    m = quat_to_matrix(q)
    return np.degrees(np.stack([m[:, 0, 2], -m[:, 1, 2], m[:, 1, 0]], -1))


def test_panning_freely_follows_the_pan_and_holds_tilt_and_roll() -> None:
    orient = _orientation(pan_with_shake)
    plan, centres = _mode_plan(orient, "pan")
    smooth, _ = _mode_plan(orient, "smooth")
    held = _pan_tilt_roll(plan.target_quats)
    smoothed = _pan_tilt_roll(smooth.target_quats)
    assert held[:, 1].std() < 0.02  # tilt held
    assert held[:, 2].std() < 0.02  # roll held
    assert held[-1, 0] - held[0, 0] == pytest.approx(
        smoothed[-1, 0] - smoothed[0, 0], rel=0.02
    )  # pan as smoothed
    assert jitter(plan.target_quats, FPS) < 0.3 * jitter(orient.at(centres), FPS)


def test_a_sideways_picture_pans_about_the_cameras_x_axis() -> None:
    orient = _orientation(lambda t: np.stack([0 * t, 0.02 + 0 * t, 0 * t], -1))  # 6 degrees about x only
    upright, _ = _mode_plan(orient, "pan")
    sideways, _ = _mode_plan(orient, "pan", display_turn=90)
    smooth, _ = _mode_plan(orient, "smooth")
    assert (
        _turn_deg(sideways.target_quats, smooth.target_quats).max() < 0.01
    )  # sideways, that turn is the pan
    assert _turn_deg(upright.target_quats, upright.target_quats[:1]).max() < 1e-3  # upright, it is tilt: held
