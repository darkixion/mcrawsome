from concurrent.futures import CancelledError
from pathlib import Path

import cv2
import numpy as np
import pytest

from mcrawsome.camera.lens import LensCalibration
from mcrawsome.motion.geometry import intrinsics, quat_from_rotvec, quat_to_matrix
from mcrawsome.motion.optical import FeatureTracks, _rays, refine_orientation, track_images
from mcrawsome.motion.orientation import Orientation
from mcrawsome.motion.plan import border_inside, plan_stabilisation


def _scene(
    moving: bool = False,
    gap: bool = False,
    readout: float = 0,
) -> tuple[Orientation, np.ndarray, FeatureTracks, np.ndarray]:
    fps = 24
    centres = 0.2 + np.arange(192) / fps
    t = np.arange(0, 8.5, 0.002)

    def rotation(time: np.ndarray, shake: bool) -> np.ndarray:
        values = np.zeros((*time.shape, 3))
        values[..., 1] = 0.02 * time  # a deliberate pan, owned by the gyro
        if shake:
            values[..., 0] += 0.004 * np.sin(2 * np.pi * 2.1 * time)
            values[..., 1] += 0.006 * np.cos(2 * np.pi * 2.7 * time)
            values[..., 2] += 0.003 * np.sin(2 * np.pi * 1.8 * time)
        return quat_from_rotvec(values)

    base = Orientation(t, rotation(t, False))
    yy, xx = np.mgrid[25:250:20, 25:460:20]
    points = np.column_stack([xx.ravel(), yy.ravel()]).astype(np.float64)
    k = intrinsics(430, 480, 270)
    world = np.column_stack([points, np.ones(len(points))]) @ np.linalg.inv(k).T
    before, after, first = [], [], []
    identity: list[int] = []
    for frame in range(len(centres) - 1):
        if gap and 65 <= frame <= 110:
            continue
        pair = []
        for index in (frame, frame + 1):
            time = np.full(len(points), centres[index])
            for _ in range(5):  # solve each observation's own rolling-shutter row time
                r = quat_to_matrix(rotation(time, True))
                rays = np.einsum("nij,nj->ni", r, world)
                xy = rays[:, :2] / rays[:, 2:] * 430 + np.array([240, 135])
                time = centres[index] + (xy[:, 1] / 270 - 0.5) * readout
            if moving:
                foreground = (points[:, 0] < 240) & (points[:, 1] > 135)
                xy[foreground, 0] += 12 * np.sin(index * 0.2)
                xy[foreground, 1] += 6 * np.cos(index * 0.32)
            pair.append(xy.astype(np.float32))
        before.append(pair[0])
        after.append(pair[1])
        first.extend([frame] * len(points))
        identity.extend((np.arange(len(points)) + (len(points) if frame > 110 and gap else 0)).tolist())
    tracks = FeatureTracks(
        np.array(first), np.array(identity), np.concatenate(before), np.concatenate(after), 480, 270
    )
    return base, centres, tracks, rotation(centres, True)


@pytest.mark.parametrize("moving", [False, True])
def test_corrects_missing_shake_while_preserving_the_gyro_pan(moving: bool) -> None:
    base, centres, tracks, truth = _scene(moving=moving)
    refined, report = refine_orientation(base, centres, 0, tracks, 430, 480, 24)
    assert report["applied"]
    observed = quat_to_matrix(truth)
    before = quat_to_matrix(base.at(centres))
    after = quat_to_matrix(refined.at(centres))
    # Compare against the independent known camera pose, rather than the fitting residuals.
    before_error = np.linalg.norm(np.diff(before - observed, axis=0), axis=(1, 2))
    after_error = np.linalg.norm(np.diff(after - observed, axis=0), axis=(1, 2))
    assert np.mean(after_error[8:-8]) < np.mean(before_error[8:-8]) * 0.25
    assert np.mean(np.linalg.norm(refined.at(centres) - base.at(centres), axis=1)) < 0.02
    plan = plan_stabilisation(refined, centres, 0.018, 1.4, 1.2, 1, 12, 480, 270, 24, 430, edge_rows=True)
    assert all(border_inside(rows, 430, 1.2, 480, 270, edge_rows=True) for rows in plan.rotations)
    np.testing.assert_allclose(
        plan.rotations.transpose(0, 1, 3, 2) @ plan.rotations,
        np.broadcast_to(np.eye(3), plan.rotations.shape),
        atol=1e-6,
    )


def test_lost_tracks_fade_to_gyro_without_crossing_the_gap() -> None:
    base, centres, tracks, _ = _scene(gap=True)
    refined, report = refine_orientation(base, centres, 0, tracks, 430, 480, 24)
    assert report["applied"]
    np.testing.assert_allclose(refined.at(centres[80:95]), base.at(centres[80:95]), atol=1e-8)


def test_pitch_opt_out_is_honoured() -> None:
    from mcrawsome.motion.geometry import quat_conjugate, quat_mul

    base, centres, tracks, _ = _scene()
    refined, _ = refine_orientation(base, centres, 0, tracks, 430, 480, 24, pitch=False)
    difference = quat_mul(refined.at(centres), quat_conjugate(base.at(centres)))
    assert np.max(abs(difference[:, 1])) < 1e-10


def test_refinement_reaches_saved_plan_and_native_row_poses(tmp_path: Path) -> None:
    from mcrawsome.camera.profile import CameraProfile
    from mcrawsome.io.gyro import GyroLog
    from mcrawsome.motion.orientation import AxisMapping
    from mcrawsome.motion.plan import StabilisationPlan
    from mcrawsome.motion.sync import SyncResult
    from mcrawsome.pipeline.stabilise import _finish
    from mcrawsome.recipe.schema import Recipe
    from mcrawsome.resolve.camera_gyro import from_plan

    base, centres, tracks, _ = _scene()
    log = GyroLog(base.times_s, np.tile([0.02, 0, 0], (len(base.times_s), 1)), None, None, "XYZ")
    fit = SyncResult(0, AxisMapping((0, 1, 2), (1, -1, 1)), 430, 0.9, (1, 1, 1), 2, True, None)
    outcome = _finish(
        fit,
        log,
        centres,
        0,
        0,
        Recipe.model_validate({"motion": {"stabilise": "on", "crop": 1.2}}),
        480,
        270,
        24,
        CameraProfile(name="test"),
        tmp_path,
        False,
        frame_global=True,
        row_bands=12,
        optical_tracks=tracks,
    )
    assert outcome.measurements["optical_correction"]["applied"]
    assert outcome.plan_path is not None
    plan = StabilisationPlan.load(outcome.plan_path)
    native = from_plan(plan, list(range(len(centres))), orientation=1, fps=24, centre_offset_s=0, rows=True)
    assert native.quaternions.shape == (len(centres), 12, 4)
    assert np.std(native.quaternions[:, :, :3]) > 0.001


def test_row_time_prediction_does_not_mistake_good_gyro_for_an_optical_error() -> None:
    base, centres, tracks, _ = _scene(readout=0.018)
    # Use the true, continuously sampled shaking orientation as the gyro.
    t = base.times_s
    values = np.column_stack(
        [
            0.004 * np.sin(2 * np.pi * 2.1 * t),
            0.02 * t + 0.006 * np.cos(2 * np.pi * 2.7 * t),
            0.003 * np.sin(2 * np.pi * 1.8 * t),
        ]
    )
    base = Orientation(t, quat_from_rotvec(values))
    refined, report = refine_orientation(base, centres, 0.018, tracks, 430, 480, 24)
    assert not report["applied"]
    assert refined is base


def test_blank_images_and_short_tracks_do_not_invent_a_correction() -> None:
    tracks = track_images([np.zeros((270, 480), np.uint8) for _ in range(20)])
    base = Orientation(np.array([0.0, 2.0]), np.array([[1, 0, 0, 0], [1, 0, 0, 0]], float))
    refined, report = refine_orientation(base, np.arange(20) / 24, 0, tracks, 430, 480, 24)
    assert refined is base
    assert not report["applied"]


def test_tracker_measures_persistent_corners_and_checks_cancellation() -> None:
    rng = np.random.default_rng(4)
    texture = cv2.GaussianBlur(rng.integers(0, 256, (270, 480), np.uint8), (3, 3), 0)
    images = [
        np.asarray(
            cv2.warpAffine(texture, np.array([[1, 0, k * 0.8], [0, 1, -k * 0.4]]), (480, 270)), np.uint8
        )
        for k in range(20)
    ]
    tracks = track_images(images)
    assert len(np.unique(tracks.identity, return_counts=True)[1]) > 100
    np.testing.assert_allclose(np.median(tracks.after - tracks.before, axis=0), [0.8, -0.4], atol=0.08)
    assert np.max(np.bincount(tracks.first)) <= 720
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise CancelledError

    with pytest.raises(CancelledError):
        track_images(images, cancel)


@pytest.mark.parametrize("model", ["opencv_standard", "opencv_fisheye"])
def test_measured_lens_rays_use_distortion_and_principal_point(model: str) -> None:
    k = np.array([[430, 0, 245], [0, 425, 129], [0, 0, 1]], float)
    lens = LensCalibration(
        model=model,
        width=960,
        height=540,
        original_width=960,
        original_height=540,
        camera_matrix=tuple(
            tuple(float(v) for v in row) for row in k * np.array([[2, 2, 2], [2, 2, 2], [1, 1, 1]])
        ),
        coefficients=(0.1, -0.02, 0, 0),
    )
    rays = np.array([[-0.3, 0.1, 1], [0.25, -0.2, 1]], float)
    if model == "opencv_fisheye":
        points = cv2.fisheye.projectPoints(
            rays.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), k, np.array(lens.coefficients)
        )[0]
    else:
        points = cv2.projectPoints(rays, np.zeros(3), np.zeros(3), k, np.array(lens.coefficients))[0]
    tracks = FeatureTracks(
        np.empty(0, int),
        np.empty(0, int),
        np.empty((0, 2), np.float32),
        np.empty((0, 2), np.float32),
        480,
        270,
    )
    actual = _rays(np.asarray(points.reshape(-1, 2), np.float64), tracks, 430, lens)
    assert np.max(abs(actual - rays)) < 1e-6


def test_exposure_average_matches_independent_blurred_feature_centroids() -> None:
    from mcrawsome.motion.optical import _predict_rays

    time = np.linspace(0, 1, 50001)
    r = np.zeros((len(time), 3))
    r[:, 2] = 4 * time**2
    base = Orientation(time, quat_from_rotvec(r))
    ta, tb = np.array([0.2, 0.4]), np.array([0.24, 0.44])
    ea, eb = np.array([0.08, 0.04]), np.array([0.015, 0.09])
    world = np.array([[0.35, -0.1, 1], [-0.3, 0.22, 1]], np.float64)

    def centroid(t: np.ndarray, exposure: np.ndarray) -> np.ndarray:
        observed = []
        for centre, shutter, ray in zip(t, exposure, world, strict=True):
            samples = np.linspace(centre - shutter / 2, centre + shutter / 2, 2001)
            angle = 4 * samples**2
            x = np.cos(angle) * ray[0] - np.sin(angle) * ray[1]
            y = np.sin(angle) * ray[0] + np.cos(angle) * ray[1]
            observed.append([np.trapezoid(x, samples) / shutter, np.trapezoid(y, samples) / shutter, 1.0])
        return np.array(observed, np.float64)

    before, expected = centroid(ta, ea), centroid(tb, eb)
    got = _predict_rays(base, ta, tb, before, ea, eb, lambda: None)
    instant = _predict_rays(base, ta, tb, before, None, None, lambda: None)
    assert np.max(abs(got - expected)) < 1e-7
    assert np.max(abs(instant - expected)) > 0.0001


def test_focus_scale_does_not_pull_rotation_on_asymmetric_tracks() -> None:
    from dataclasses import replace

    base, centres, tracks, truth = _scene()
    zoom = 1 + 0.025 * np.sin(2 * np.pi * 1.6 * centres)
    p, q = tracks.before.astype(np.float64), tracks.after.astype(np.float64)
    p = (p - [240, 135]) * zoom[tracks.first, None] + [240, 135]
    q = (q - [240, 135]) * zoom[tracks.first + 1, None] + [240, 135]
    # Uneven feature coverage makes a scale change look like a turn to the old model.
    keep = tracks.before[:, 0] < 285
    changed = replace(
        tracks,
        first=tracks.first[keep],
        identity=tracks.identity[keep],
        before=p[keep].astype(np.float32),
        after=q[keep].astype(np.float32),
    )
    old, _ = refine_orientation(base, centres, 0, changed, 430, 480, 24)
    new, report = refine_orientation(
        base, centres, 0, changed, 430, 480, 24, focus_distance=np.linspace(1, 4, len(centres))
    )
    expected = quat_to_matrix(truth)
    old_error = np.diff(quat_to_matrix(old.at(centres)) - expected, axis=0)
    new_error = np.diff(quat_to_matrix(new.at(centres)) - expected, axis=0)
    assert report["focus_scale_separated"] is True
    assert np.mean(new_error[10:-10] ** 2) < np.mean(old_error[10:-10] ** 2) * 0.5


@pytest.mark.parametrize("field", ["exposure", "focus"])
def test_refinement_rejects_misaligned_capture_metadata(field: str) -> None:
    base, centres, tracks, _ = _scene()
    with pytest.raises(ValueError, match=r"exposure|Focus"):
        refine_orientation(
            base,
            centres,
            0,
            tracks,
            430,
            480,
            24,
            exposures_s=np.ones(1) if field == "exposure" else None,
            focus_distance=np.ones(1) if field == "focus" else None,
        )


def test_fixed_and_unknown_focus_retain_the_existing_rotation_model() -> None:
    base, centres, tracks, _ = _scene()
    original, _ = refine_orientation(base, centres, 0, tracks, 430, 480, 24)
    fixed, report = refine_orientation(
        base, centres, 0, tracks, 430, 480, 24, focus_distance=np.ones(len(centres))
    )
    assert report["focus_scale_separated"] is False
    np.testing.assert_array_equal(fixed.at(centres), original.at(centres))


def test_one_early_focus_change_does_not_change_later_rotation_estimates() -> None:
    base, centres, tracks, truth = _scene(moving=True)
    original, _ = refine_orientation(base, centres, 0, tracks, 430, 480, 24)
    focus = np.full(len(centres), 3.0)
    focus[:24] = np.linspace(1, 3, 24)
    guarded, _ = refine_orientation(base, centres, 0, tracks, 430, 480, 24, focus_distance=focus)
    expected = quat_to_matrix(truth)
    old_error = np.diff(quat_to_matrix(original.at(centres)) - expected, axis=0)
    new_error = np.diff(quat_to_matrix(guarded.at(centres)) - expected, axis=0)
    # Known camera motion after the focus event must retain the simpler model's accuracy.
    assert np.mean(new_error[130:] ** 2) <= np.mean(old_error[130:] ** 2) * 1.001


def test_a_scale_candidate_that_loses_on_held_out_features_keeps_the_simple_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcrawsome.motion.optical as optical

    base, centres, tracks, _ = _scene()
    original, _ = refine_orientation(base, centres, 0, tracks, 430, 480, 24)
    fit = optical._fit_rotation

    def misleading_scale(
        points: np.ndarray, residual: np.ndarray, cells: np.ndarray, scale: bool = False
    ) -> tuple[np.ndarray, float]:
        rotation, error = fit(points, residual, cells)
        # Inject an overfit candidate whose advertised training error is excellent.
        # Its zoom prediction disagrees with the independent, unchanged observations.
        return (np.r_[rotation, 0.05], 0.0) if scale else (rotation, error)

    monkeypatch.setattr(optical, "_fit_rotation", misleading_scale)
    guarded, report = refine_orientation(
        base, centres, 0, tracks, 430, 480, 24, focus_distance=np.linspace(1, 4, len(centres))
    )
    np.testing.assert_array_equal(guarded.at(centres), original.at(centres))
    assert report["focus_scale_pairs"] == 0


def test_missing_focus_samples_do_not_invent_a_focus_transition() -> None:
    base, centres, tracks, _ = _scene()
    original, _ = refine_orientation(base, centres, 0, tracks, 430, 480, 24)
    focus = np.full(len(centres), 3.0)
    focus[:24], focus[24:48] = 1, np.nan
    guarded, report = refine_orientation(base, centres, 0, tracks, 430, 480, 24, focus_distance=focus)
    assert report["focus_scale_separated"] is False
    np.testing.assert_array_equal(guarded.at(centres), original.at(centres))


@pytest.mark.parametrize("edge", ["start", "end"])
def test_refinement_does_not_snap_to_zero_when_tracks_end(edge: str) -> None:
    base, centres, tracks, _ = _scene()
    refined, report = refine_orientation(base, centres, 0, tracks, 430, 480, 24)
    assert report["applied"]
    # Track filtering intentionally leaves two unmeasured frames at either end.
    # They must continue the adjacent correction, not jump back to bare gyro.
    from mcrawsome.motion.optical import RefinedOrientation

    assert isinstance(refined, RefinedOrientation)
    correction = refined.corrections[:3] if edge == "start" else refined.corrections[-3:]
    anchor = correction[-1] if edge == "start" else correction[0]
    assert np.linalg.norm(anchor) > np.radians(0.1)
    assert np.max(np.linalg.norm(np.diff(correction, axis=0), axis=1)) < np.radians(0.1)
    assert np.min(np.linalg.norm(correction, axis=1)) > np.linalg.norm(anchor) * 0.7
