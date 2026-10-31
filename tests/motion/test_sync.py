import numpy as np
import pytest

from mcrawsome.camera.profile import Axes, CameraProfile
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.flow import MotionTrack
from mcrawsome.motion.sync import STRIPS, cumulative_rotation, fit_readout, fit_sync

RATE, FPS, TRUE_OFFSET, FOCAL = 400.0, 24.0, -0.104, 3000.0
AXES, SIGNS = (0, 1, 2), (1, -1, 1)


def _gyro(seed: int = 3, seconds: float = 14.0) -> GyroLog:
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 1 / RATE)
    raw = rng.standard_normal((len(t), 3))
    kernel = np.exp(-0.5 * (np.arange(-60, 61) / 20) ** 2)
    smooth = np.stack([np.convolve(raw[:, k], kernel / kernel.sum(), "same") for k in range(3)], -1) * 1.5
    return GyroLog(times_s=t, gyro=smooth, accel=None, start_epoch_s=None, orientation="XYZ", clock="video")


def _frames(n: int = 240) -> np.ndarray:
    return 2.0 + np.arange(n) / FPS


def _measured(log: GyroLog, frames: np.ndarray, focal: float, noise: float, seed: int = 5) -> MotionTrack:
    centre = frames + 0.5 / FPS / 2 + 0.009 + TRUE_OFFSET  # exposure/2 + readout/2 + offset
    cum = cumulative_rotation(log)
    rot = np.stack(
        [
            np.interp(centre[1:], log.times_s, cum[:, k]) - np.interp(centre[:-1], log.times_s, cum[:, k])
            for k in range(3)
        ],
        -1,
    )
    rng = np.random.default_rng(seed)
    dx = SIGNS[0] * focal * rot[:, AXES[0]] + rng.normal(0, noise, len(rot))
    dy = SIGNS[1] * focal * rot[:, AXES[1]] + rng.normal(0, noise, len(rot))
    roll = SIGNS[2] * rot[:, AXES[2]] + rng.normal(0, noise / focal, len(rot))
    return MotionTrack(np.arange(len(rot)), dx, dy, roll)


def _fit(track: MotionTrack, log: GyroLog, seed: CameraProfile | None = None) -> object:
    return fit_sync(
        log, _frames(), exposure_s=0.5 / FPS, motion=track, seed=seed, kind="gpmf", readout_s=0.018
    )


def test_free_fit_recovers_offset_axes_and_focal() -> None:
    log = _gyro()
    result = fit_sync(log, _frames(), 0.5 / FPS, _measured(log, _frames(), FOCAL, 1.0), None, "gpmf", 0.018)
    assert result.offset_s == pytest.approx(TRUE_OFFSET, abs=0.002)
    assert result.axes.axes == AXES
    assert result.axes.signs == SIGNS
    assert result.focal_px == pytest.approx(FOCAL, rel=0.03)
    assert result.confident
    assert not result.constrained


def test_sync_uses_each_frames_exposure_during_shutter_changes_recorded_contract() -> None:
    log, frames = _gyro(), _frames()
    exposure = np.where(np.arange(len(frames)) % 2 == 0, 0.002, 0.035)
    centre = frames + exposure / 2 + 0.009 + TRUE_OFFSET
    cum = cumulative_rotation(log)
    rotations = np.stack([np.diff(np.interp(centre, log.times_s, cum[:, k])) for k in range(3)], -1)
    track = MotionTrack(
        np.arange(len(frames) - 1), FOCAL * rotations[:, 0], -FOCAL * rotations[:, 1], rotations[:, 2]
    )
    seed = CameraProfile(
        name="test", focal_px=FOCAL, gpmf_axes=Axes(axes=AXES, signs=SIGNS), gpmf_sync_ms=TRUE_OFFSET * 1000
    )
    result = fit_sync(log, frames, exposure, track, seed, "gpmf", 0.018, min_confidence=0, focal_known=True)
    wrong = fit_sync(
        log, frames, float(exposure[0]), track, seed, "gpmf", 0.018, min_confidence=0, focal_known=True
    )
    assert result.offset_s == pytest.approx(TRUE_OFFSET, abs=0.00051)
    assert result.confidence > 0.9999
    assert wrong.confidence < result.confidence - 0.05


def test_sensor_frame_intervals_keep_nanosecond_precision_after_epoch_alignment_recorded_contract() -> None:
    from dataclasses import replace

    from mcrawsome.motion.sync import raw_frame_times

    stamps = 111_395_082_497_854 + np.arange(120, dtype=np.int64) * 41_663_984
    received = 1_792_339_400_000 + (stamps - stamps[0]) // 1_000_000 + 54
    log = replace(_gyro(), start_epoch_s=1_792_339_400.0)
    times = raw_frame_times(stamps, received, log)
    np.testing.assert_allclose(np.diff(times), 0.041663984, rtol=0, atol=1e-14)


@pytest.mark.parametrize("sparse", [False, True])
def test_sync_samples_each_used_frame_boundary_once_per_axis(
    monkeypatch: pytest.MonkeyPatch, sparse: bool
) -> None:
    log, frames = _gyro(), _frames()
    track = _measured(log, frames, FOCAL, 1.0)
    if sparse:
        selected = np.array([20, 3, 4, 19, 8, 100, 101, 120])
        track = MotionTrack(
            track.first[selected], track.dx[selected], track.dy[selected], track.roll[selected]
        )
    expected = fit_sync(log, frames, 0.5 / FPS, track, None, "gpmf", 0.018)
    needed = len(np.unique(np.concatenate([track.first, track.first + 1])))
    interp = np.interp
    queried: list[int] = []

    def counted(x: np.ndarray, xp: np.ndarray, fp: np.ndarray) -> np.ndarray:
        queried.append(len(x))
        return np.asarray(interp(x, xp, fp), dtype=np.float64)

    monkeypatch.setattr(np, "interp", counted)
    actual = fit_sync(log, frames, 0.5 / FPS, track, None, "gpmf", 0.018)
    assert actual == expected
    assert queried
    assert set(queried) == {needed}


def test_profile_constrains_the_fit() -> None:
    log = _gyro()
    seed = CameraProfile(
        name="p", focal_px=3000.0, gpmf_axes=Axes(axes=AXES, signs=SIGNS), gpmf_sync_ms=-90.0
    )
    track = _measured(log, _frames(), 2900.0, 1.0)
    result = fit_sync(log, _frames(), 0.5 / FPS, track, seed, "gpmf", 0.018)
    assert result.constrained
    assert result.offset_s == pytest.approx(TRUE_OFFSET, abs=0.002)
    assert result.focal_px == pytest.approx(2900.0, rel=0.03)


def test_free_fit_confidence_scores_the_mapping_used_by_stabilization() -> None:
    # A general 3x3 least-squares fit can explain this mixed motion almost
    # perfectly; the signed-axis model used to stabilise it cannot.
    log = _gyro()
    clean = _measured(log, _frames(), FOCAL, 0.01)
    track = MotionTrack(clean.first, clean.dx + 0.98 * clean.dy, clean.dy - 0.98 * clean.dx, clean.roll)
    result = fit_sync(log, _frames(), 0.5 / FPS, track, None, "gpmf", 0.018)
    centre = _frames() + 0.5 / FPS / 2 + 0.009 + result.offset_s
    cum = cumulative_rotation(log)
    gyro = np.stack(
        [
            np.interp(centre[1:], log.times_s, cum[:, k]) - np.interp(centre[:-1], log.times_s, cum[:, k])
            for k in range(3)
        ],
        -1,
    )
    pred = np.stack(
        [result.axes.signs[k] * gyro[:, result.axes.axes[k]] * result.focal_px for k in range(2)], -1
    )
    meas = np.stack([track.dx, track.dy], -1)
    actual_confidence = 1 - ((meas - pred) ** 2).sum() / ((meas - meas.mean(0)) ** 2).sum()
    assert result.confidence == pytest.approx(actual_confidence)
    assert result.confidence < 0.6
    assert result.correlation[0] < 0.9
    assert not result.confident
    assert result.reason is not None
    assert "do not agree" in result.reason


def test_noise_is_not_trusted() -> None:
    log = _gyro()
    rng = np.random.default_rng(1)
    n = len(_frames()) - 1
    track = MotionTrack(np.arange(n), rng.normal(0, 20, n), rng.normal(0, 20, n), rng.normal(0, 0.01, n))
    result = fit_sync(log, _frames(), 0.5 / FPS, track, None, "gpmf", 0.018)
    assert not result.confident
    assert result.reason is not None


def test_a_still_scene_is_reported() -> None:
    log = _gyro()
    track = _measured(
        GyroLog(log.times_s, log.gyro * 0.0005, None, None, "XYZ", clock="video"), _frames(), FOCAL, 0.05
    )
    result = fit_sync(log, _frames(), 0.5 / FPS, track, None, "gpmf", 0.018)
    assert not result.confident
    assert result.reason is not None
    assert "barely moves" in result.reason


def _strip(log: GyroLog, frames: np.ndarray, row: float, readout: float, seed: int) -> MotionTrack:
    """Motion seen by the rows at `row` (0 top, 1 bottom) of a rolling-shutter sensor."""
    shifted = frames + row * readout - 0.009  # _measured adds the 9 ms of a mid-frame centre
    return _measured(log, shifted, FOCAL, 0.5, seed)


def test_readout_is_recovered_from_the_top_and_bottom_thirds() -> None:
    log = _gyro(seconds=30.0)
    frames = 2.0 + np.arange(600) / FPS
    top, bottom = (_strip(log, frames, sum(rows) / 2, 0.018, i) for i, rows in enumerate(STRIPS))
    seed = CameraProfile(
        name="c", focal_px=FOCAL, gpmf_axes=Axes(axes=AXES, signs=SIGNS), gpmf_sync_ms=-100.0
    )
    readout, offset = fit_readout(log, frames, 0.5 / FPS, top, bottom, seed, "gpmf")
    assert readout == pytest.approx(0.018, abs=0.002)
    assert offset == pytest.approx(TRUE_OFFSET, abs=0.002)  # for a frame centre with that readout


@pytest.mark.local
def test_matches_the_prototype_on_a_well_measured_clip() -> None:
    from conftest import LOCAL_DIR

    from mcrawsome.io.clip import Clip
    from mcrawsome.io.gcsv import read_gcsv
    from mcrawsome.motion.flow import measure_motion_raw
    from mcrawsome.motion.sync import raw_frame_times

    clip_path = LOCAL_DIR / "261004_174022_VIDEO_26mm.mcraw"
    if not clip_path.exists():
        pytest.skip("local clip not present")
    log = read_gcsv(clip_path.with_suffix(".gcsv"))
    with Clip(clip_path) as clip:
        received = [clip.frame_metadata(i).received_ms for i in range(clip.frame_count)]
        times = raw_frame_times(clip.timestamps_ns, np.array(received), log)
        exposure = clip.frame_metadata(0).exposure_time_ns / 1e9
        middle = clip.frame_count // 2
    indices = list(range(middle - 100, middle + 100))
    track = measure_motion_raw(clip_path, indices, workers=4)
    result = fit_sync(log, times, exposure, track, None, "gcsv", readout_s=0.012)  # the prototype's readout
    assert result.offset_s == pytest.approx(-0.096, abs=0.005)
    assert result.axes.axes == (0, 1, 2)
    assert result.axes.signs == (1, -1, 1)
    assert result.confident


def test_a_free_fit_that_reuses_a_gyro_axis_is_not_trusted() -> None:
    log = _gyro()
    track = _measured(log, _frames(), FOCAL, 1.0)
    rng = np.random.default_rng(8)
    same = MotionTrack(track.first, track.dx, -track.dx + rng.normal(0, 1.0, len(track.dx)), track.roll)
    result = fit_sync(log, _frames(), 0.5 / FPS, same, None, "gpmf", 0.018)
    assert len(set(result.axes.axes)) < 3
    assert result.reason is not None
    assert "axes" in result.reason


def _with_drift(track: MotionTrack, scale: float, seed: int = 9) -> MotionTrack:
    """Add slow image motion the gyro does not see (a moving camera's parallax, moving subjects)."""
    rng = np.random.default_rng(seed)
    n = len(track.dx)
    kernel = np.exp(-0.5 * (np.arange(-40, 41) / 12) ** 2)
    drift = [np.convolve(rng.standard_normal(n), kernel / kernel.sum(), "same") for _ in range(2)]
    drift = [d / d.std() * scale for d in drift]
    return MotionTrack(track.first, track.dx + drift[0], track.dy + drift[1], track.roll)


def _profile_seed(sync_ms: float = -100.0) -> CameraProfile:
    return CameraProfile(
        name="p", focal_px=FOCAL, gpmf_axes=Axes(axes=AXES, signs=SIGNS), gpmf_sync_ms=sync_ms
    )


def test_a_moving_camera_is_trusted_on_timing_when_the_profile_agrees() -> None:
    log = _gyro()
    clean = _measured(log, _frames(), FOCAL, 1.0)
    track = _with_drift(clean, 2.0 * float(np.std(clean.dx)))
    result = fit_sync(log, _frames(), 0.5 / FPS, track, _profile_seed(), "gpmf", 0.018)
    assert result.confidence < 0.6  # the picture alone would not be trusted
    assert result.confident
    assert result.offset_s == pytest.approx(TRUE_OFFSET, abs=0.005)  # the plan's acceptance tolerance
    assert result.focal_px == FOCAL  # the fitted scale is unreliable here, so the profile's is used
    assert result.note is not None
    assert "profile" in result.note


def test_a_log_from_another_clip_is_not_trusted_even_with_a_profile() -> None:
    other = _gyro(seed=11)
    track = _with_drift(_measured(_gyro(), _frames(), FOCAL, 1.0), 20.0)
    result = fit_sync(other, _frames(), 0.5 / FPS, track, _profile_seed(), "gpmf", 0.018)
    assert not result.confident


def _profile_with(signs: tuple[int, int, int]) -> CameraProfile:
    axes = Axes(axes=AXES, signs=signs)
    return CameraProfile(name="p", focal_px=FOCAL, gpmf_axes=axes, gpmf_sync_ms=-100.0)


@pytest.mark.parametrize("drift", [1.0, 2.0])
def test_a_profile_with_a_wrong_axis_sign_is_never_trusted(drift: float) -> None:
    # reviewer's scenario: mostly horizontal shake, the profile's image-y sign wrong, a moving camera
    log = _gyro()
    log = GyroLog(log.times_s, log.gyro * np.array([1.0, 0.3, 1.0]), None, None, "XYZ", clock="video")
    clean = _measured(log, _frames(), FOCAL, 1.0)
    track = _with_drift(clean, drift * float(np.std(clean.dx)))
    result = fit_sync(log, _frames(), 0.5 / FPS, track, _profile_with((1, 1, 1)), "gpmf", 0.018)
    assert not result.confident


def test_a_profile_with_a_wrong_roll_sign_is_not_trusted() -> None:
    log = _gyro()
    track = _measured(log, _frames(), FOCAL, 0.5)
    result = fit_sync(log, _frames(), 0.5 / FPS, track, _profile_with((1, -1, -1)), "gpmf", 0.018)
    assert not result.confident
    assert result.reason is not None
    assert "roll" in result.reason
    assert "Possible causes" in result.reason
    assert "display orientation" in result.reason


def test_a_still_scene_is_not_rescued_by_the_profile() -> None:
    log = _gyro()
    still = GyroLog(log.times_s, log.gyro * 0.0005, None, None, "XYZ", clock="video")
    track = _measured(still, _frames(), FOCAL, 0.05)
    result = fit_sync(log, _frames(), 0.5 / FPS, track, _profile_with(SIGNS), "gpmf", 0.018)
    assert not result.confident
    assert result.reason is not None
    assert "barely moves" in result.reason


@pytest.mark.parametrize(
    ("measured", "expected"), [(1500.0, 3000.0 / 1.25), (2900.0, 2900.0), (4500.0, 3750.0)]
)
def test_a_known_focal_length_only_moves_within_its_tolerance(measured: float, expected: float) -> None:
    """A walking shot's parallax made the picture move half as far as the lens implies (261018_170320:
    1510 px fitted against 2955 px from its 25.37 mm lens); a focal length from the profile or the clip's
    lens metadata is a measurement, so the fit may adjust it by at most FOCAL_TOLERANCE."""
    log = _gyro()
    seed = CameraProfile(
        name="p", focal_px=3000.0, gpmf_axes=Axes(axes=AXES, signs=SIGNS), gpmf_sync_ms=-100.0
    )
    track = _measured(log, _frames(), measured, 1.0)
    result = fit_sync(log, _frames(), 0.5 / FPS, track, seed, "gpmf", 0.018, focal_known=True)
    assert result.focal_px == pytest.approx(expected, rel=0.03)
    free = fit_sync(log, _frames(), 0.5 / FPS, track, seed, "gpmf", 0.018)
    assert free.focal_px == pytest.approx(measured, rel=0.03)  # without a known focal length, as before


def test_the_sync_window_is_where_the_camera_turns_most() -> None:
    from mcrawsome.motion.sync import turning_window

    t = np.arange(0.0, 60.0, 1 / 400)
    gyro = np.random.default_rng(3).normal(0, 0.01, (len(t), 3))
    gyro[(t > 42) & (t < 46), 0] += 0.4  # a four-second pan late in the clip
    log = GyroLog(t, gyro, None, None, "XYZ", clock="video")
    frames = np.arange(1440) / 24.0
    start = turning_window(log, frames, 480)
    assert start <= 42 * 24  # the window holds the whole pan
    assert start + 480 >= 46 * 24
    short = turning_window(log, frames, 120)
    assert abs((short + 60) / 24 - 44) < 0.5  # a window shorter than the pan sits on it
    assert turning_window(log, frames, 5000) == 0  # a window longer than the clip starts at its first frame


def test_an_axis_whose_full_correction_adds_shake_is_scaled_to_what_removes_most() -> None:
    """Walking: the phone bobs while the hand tilts it, so the picture moves up and down less than the
    camera tilts. Full tilt correction would show the bob; the fit scales that axis instead of refusing."""
    log = _gyro()
    seed = CameraProfile(
        name="p", focal_px=3000.0, gpmf_axes=Axes(axes=AXES, signs=SIGNS), gpmf_sync_ms=-104.0
    )
    track = _measured(log, _frames(), FOCAL, 0.5)
    walking = MotionTrack(track.first, track.dx, 0.25 * track.dy, track.roll)  # 75% of the tilt cancelled
    result = fit_sync(log, _frames(), 0.5 / FPS, walking, seed, "gpmf", 0.018, focal_known=True)
    assert result.reason is None
    assert result.gains[0] == 1.0
    assert result.gains[2] == 1.0
    # the share of the fitted focal length's tilt correction the picture shows
    assert result.gains[1] == pytest.approx(0.25 * FOCAL / result.focal_px, abs=0.05)
    assert result.note is not None
    assert "up-and-down correction is scaled to" in result.note
    full = fit_sync(log, _frames(), 0.5 / FPS, track, seed, "gpmf", 0.018, focal_known=True)
    assert full.gains == (1.0, 1.0, 1.0)  # an axis the correction improves keeps all of it


def test_the_camera_path_turns_each_axis_by_its_gain() -> None:
    from mcrawsome.motion.orientation import AxisMapping, Orientation

    log = _gyro()
    mapping = AxisMapping(AXES, SIGNS)
    full = Orientation.from_gyro(log, mapping)
    no_tilt = Orientation.from_gyro(log, mapping, pitch=False)
    np.testing.assert_allclose(
        Orientation.from_gyro(log, mapping, gains=(1.0, 0.0, 1.0)).quats, no_tilt.quats
    )
    np.testing.assert_allclose(Orientation.from_gyro(log, mapping, gains=(1.0, 1.0, 1.0)).quats, full.quats)
    half = Orientation.from_gyro(log, mapping, gains=(1.0, 0.5, 1.0))
    assert not np.allclose(half.quats, full.quats)
    assert not np.allclose(half.quats, no_tilt.quats)


def test_sync_uses_each_frames_exposure_during_shutter_changes() -> None:
    log, frames = _gyro(), _frames()
    exposure = np.where(np.arange(len(frames)) % 2 == 0, 0.002, 0.035)
    centre = frames + exposure / 2 + 0.009 + TRUE_OFFSET
    cum = cumulative_rotation(log)
    rotations = np.stack([np.diff(np.interp(centre, log.times_s, cum[:, k])) for k in range(3)], -1)
    track = MotionTrack(
        np.arange(len(frames) - 1), FOCAL * rotations[:, 0], -FOCAL * rotations[:, 1], rotations[:, 2]
    )
    seed = CameraProfile(
        name="test", focal_px=FOCAL, gpmf_axes=Axes(axes=AXES, signs=SIGNS), gpmf_sync_ms=TRUE_OFFSET * 1000
    )
    result = fit_sync(log, frames, exposure, track, seed, "gpmf", 0.018, min_confidence=0, focal_known=True)
    wrong = fit_sync(
        log, frames, float(exposure[0]), track, seed, "gpmf", 0.018, min_confidence=0, focal_known=True
    )
    assert result.offset_s == pytest.approx(TRUE_OFFSET, abs=0.00051)
    assert result.confidence > 0.9999
    assert wrong.confidence < result.confidence - 0.05


def test_sensor_frame_intervals_keep_nanosecond_precision_after_epoch_alignment() -> None:
    from dataclasses import replace

    from mcrawsome.motion.sync import raw_frame_times

    stamps = 111_395_082_497_854 + np.arange(120, dtype=np.int64) * 41_663_984
    received = 1_792_339_400_000 + (stamps - stamps[0]) // 1_000_000 + 54
    log = replace(_gyro(), start_epoch_s=1_792_339_400.0)
    times = raw_frame_times(stamps, received, log)
    np.testing.assert_allclose(np.diff(times), 0.041663984, rtol=0, atol=1e-14)


def _recorded_seed() -> CameraProfile:
    return CameraProfile(name="recorded", focal_px=FOCAL, mcraw_axes=Axes(axes=AXES, signs=SIGNS))


def test_recorded_motion_keeps_its_own_timing_axes_and_focal_length() -> None:
    # Measured 104 ms late and at a 10% longer focal length: neither moves the recording's own.
    log = _gyro()
    track = _measured(log, _frames(), FOCAL * 1.1, 1.0)
    result = fit_sync(log, _frames(), 0.5 / FPS, track, _recorded_seed(), "mcraw", 0.018, focal_known=True)
    assert result.offset_s == 0
    assert result.focal_px == FOCAL
    assert result.axes.axes == AXES
    assert result.axes.signs == SIGNS
    assert result.constrained
    assert result.confident  # what the picture cannot explain is not the recording's timing


def test_recorded_motion_scales_an_axis_against_the_picture_instead_of_refusing() -> None:
    log = _gyro()
    track = _measured(log, _frames() - TRUE_OFFSET, FOCAL, 1.0)  # on the recording's own clock
    against = MotionTrack(track.first, track.dx, -track.dy, track.roll)  # the picture moves the other way
    recorded = fit_sync(log, _frames(), 0.5 / FPS, against, _recorded_seed(), "mcraw", 0.018)
    assert recorded.confident
    assert recorded.gains[1] < 0.1  # up and down is all but switched off
    assert recorded.gains[0] == 1.0
    logged = CameraProfile(name="p", focal_px=FOCAL, gcsv_axes=Axes(axes=AXES, signs=SIGNS), gcsv_sync_ms=0)
    from_log = fit_sync(log, _frames(), 0.5 / FPS, against, logged, "gcsv", 0.018)
    assert not from_log.confident  # a .gcsv's timing or axes might be wrong: refused as before


def test_recorded_motion_without_axes_is_fitted_freely_near_its_own_clock() -> None:
    log = _gyro()
    track = _measured(log, _frames() - TRUE_OFFSET, FOCAL, 1.0)  # on the recording's own clock
    seed = CameraProfile(name="recorded", focal_px=FOCAL)  # no lens facing recorded: axes unknown
    result = fit_sync(log, _frames(), 0.5 / FPS, track, seed, "mcraw", 0.018)
    assert not result.constrained
    assert result.offset_s == pytest.approx(0, abs=0.002)
    assert result.axes.axes == AXES
    assert result.axes.signs == SIGNS
