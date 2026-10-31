from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mcrawsome.camera.profile import CameraProfile
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.orientation import AxisMapping
from mcrawsome.motion.sync import SyncResult
from mcrawsome.pipeline.stabilise import _finish
from mcrawsome.recipe.schema import Recipe


def test_a_gyro_timeline_keeps_an_unconfirmed_fit_and_generates_its_corrections(tmp_path: Path) -> None:
    from dataclasses import replace

    from mcrawsome.motion.plan import StabilisationPlan
    from mcrawsome.pipeline.stabilise import StabiliseError

    fit = replace(_fit(), reason="the correction would increase side-to-side and roll shake")
    t = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(t, np.random.default_rng(2).normal(0, 0.05, (len(t), 3)), None, None, "XYZ")
    frames = 0.2 + np.arange(96) / 24.0
    recipe = Recipe.model_validate({"motion": {"stabilise": "on"}})
    arguments = (
        fit,
        log,
        frames,
        0.02,
        0.018,
        recipe,
        960,
        540,
        24.0,
        CameraProfile(name="p"),
        tmp_path,
        False,
    )
    with pytest.raises(StabiliseError, match="increase side-to-side"):
        _finish(*arguments, frame_global=True)
    assert not (tmp_path / "stabilisation.npz").exists()
    outcome = _finish(*arguments, frame_global=True, allow_unverified_sync=True)
    assert outcome.plan_path is not None
    plan = StabilisationPlan.load(outcome.plan_path)
    assert plan.rotations.shape == (96, 1, 3, 3)
    assert np.isfinite(plan.rotations).all()
    assert not np.allclose(plan.rotations, np.eye(3))
    assert outcome.gyro is not None
    assert outcome.gyro.fit == fit
    assert not outcome.gyro.fit.confident
    assert outcome.measurements["sync_check_bypassed"] is True
    assert outcome.measurements["sync_rejection_reason"] == fit.reason
    assert fit.reason is not None
    assert any(fit.reason in note and "could not confirm" in note for note in outcome.notes)
    confirmed = _finish(_fit(), *arguments[1:], frame_global=True, allow_unverified_sync=True)
    assert "sync_check_bypassed" not in confirmed.measurements  # nothing to say when the picture agrees
    assert not any("could not confirm" in note for note in confirmed.notes)


def test_unchecked_sync_is_not_enabled_for_physical_exports(tmp_path: Path) -> None:
    from mcrawsome.pipeline.stabilise import StabiliseError, plan_raw_clip

    with pytest.raises(StabiliseError, match="frame-global timeline"):
        plan_raw_clip(tmp_path / "missing.mcraw", Recipe(), 1, tmp_path, allow_unverified_sync=True)


def _fit() -> SyncResult:
    return SyncResult(0.0, AxisMapping((0, 1, 2), (1, -1, 1)), 750.0, 0.9, (0.9, 0.9, 0.9), 2.0, True, None)


@pytest.mark.parametrize("enabled", [False, True])
def test_native_horizon_lock_reaches_the_saved_plan(enabled: bool, tmp_path: Path) -> None:
    from mcrawsome.camera.profile import Axes
    from mcrawsome.motion.plan import StabilisationPlan

    t = np.arange(0, 5, 0.005)
    a = np.radians(4)
    accel = np.tile([9.80665 * np.cos(a), 9.80665 * np.sin(a), 0], (len(t), 1))
    log = GyroLog(t, np.zeros_like(accel), accel, None, "XYZ")
    profile = CameraProfile(name="test", gcsv_gravity_axes=Axes(axes=(1, 0, 2), signs=(-1, -1, -1)))
    recipe = Recipe.model_validate({"motion": {"stabilise": "on", "horizon_lock": enabled, "crop": 1.2}})
    result = _finish(
        _fit(),
        log,
        0.2 + np.arange(96) / 24,
        0.02,
        0.018,
        recipe,
        960,
        540,
        24,
        profile,
        tmp_path,
        False,
        frame_global=True,
    )
    assert result.plan_path is not None
    plan = StabilisationPlan.load(result.plan_path)
    assert plan.mean_correction_deg == pytest.approx(4 if enabled else 0, abs=0.01)
    assert result.measurements is not None
    assert result.measurements.get("horizon_lock", False) == enabled


def test_native_horizon_lock_requires_a_verified_mapping(tmp_path: Path) -> None:
    from mcrawsome.pipeline.stabilise import StabiliseError

    t = np.arange(0, 5, 0.005)
    log = GyroLog(t, np.zeros((len(t), 3)), np.tile([0, 9.80665, 0], (len(t), 1)), None, "XYZ")
    with pytest.raises(StabiliseError, match="verified accelerometer"):
        _finish(
            _fit(),
            log,
            0.2 + np.arange(96) / 24,
            0.02,
            0.018,
            Recipe.model_validate({"motion": {"horizon_lock": True}}),
            960,
            540,
            24,
            CameraProfile(name="unknown"),
            tmp_path,
            False,
            frame_global=True,
        )


def _outcome(times_s: np.ndarray, tmp_path: Path) -> list[str]:
    rng = np.random.default_rng(1)
    gyro = rng.normal(0, 0.05, (len(times_s), 3))
    log = GyroLog(times_s, gyro, None, None, "XYZ", clock="video")
    frames = 0.2 + np.arange(96) / 24.0
    outcome = _finish(
        _fit(), log, frames, 0.02, 0.018, Recipe(), 960, 540, 24.0, CameraProfile(name="p"), tmp_path, False
    )
    assert outcome.plan_path is not None
    return outcome.notes


def test_frames_the_log_misses_are_noted(tmp_path: Path) -> None:
    t = np.arange(0.0, 5.0, 1 / 400)
    notes = _outcome(t[(t < 2.0) | (t > 2.6)], tmp_path)
    assert any("gyro log misses" in note for note in notes), notes


def test_a_complete_log_adds_no_coverage_note(tmp_path: Path) -> None:
    notes = _outcome(np.arange(0.0, 5.0, 1 / 400), tmp_path)
    assert not any("gyro log misses" in note for note in notes), notes


@pytest.mark.fixture
def test_a_log_that_misses_most_of_the_clip_switches_stabilisation_off(
    fixture_12bit: Path, tmp_path: Path
) -> None:
    from mcrawsome.io.clip import Clip
    from mcrawsome.io.gcsv import read_gcsv
    from mcrawsome.motion.gyroflow import synced_gcsv
    from mcrawsome.motion.sync import raw_frame_times
    from mcrawsome.pipeline.stabilise import plan_raw_clip

    log = read_gcsv(fixture_12bit.with_suffix(".gcsv"))
    with Clip(fixture_12bit) as clip:
        received = np.array([clip.frame_metadata(i).received_ms for i in range(clip.frame_count)], float)
        times = raw_frame_times(clip.timestamps_ns, received, log)
    keep = log.times_s < times[0] + 0.01  # the log stops just after the first frame starts
    short = GyroLog(log.times_s[keep], log.gyro[keep], None, log.start_epoch_s, log.orientation, log.header)
    clip_path = tmp_path / "clip.mcraw"
    clip_path.symlink_to(fixture_12bit)
    clip_path.with_suffix(".gcsv").write_text(synced_gcsv(short, 0.0), encoding="utf-8")
    outcome = plan_raw_clip(clip_path, Recipe(), 1, tmp_path)
    assert outcome.plan_path is None
    assert any("covers only" in note for note in outcome.notes), outcome.notes


def _decide(
    gyro_scale: float, stabilise: str, tmp_path: Path, seed: int = 2
) -> tuple[Path | None, list[str]]:
    from mcrawsome.motion.flow import MotionTrack

    rng = np.random.default_rng(seed)
    t = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(t, rng.normal(0, 0.05, (len(t), 3)) * gyro_scale, None, None, "XYZ", clock="video")
    frames = 0.2 + np.arange(96) / 24.0
    track = MotionTrack(np.arange(95), rng.normal(0, 3.0, 95), rng.normal(0, 3.0, 95), np.zeros(95))
    recipe = Recipe.model_validate({"motion": {"stabilise": stabilise}})
    outcome = _finish(
        _fit(), log, frames, 0.02, 0.018, recipe, 960, 540, 24.0, CameraProfile(name="p"), tmp_path, False,
        track=track,
    )  # fmt: skip
    return outcome.plan_path, outcome.notes


def test_auto_skips_a_plan_that_removes_little_shake(tmp_path: Path) -> None:
    plan_path, notes = _decide(1.0, "auto", tmp_path)
    assert plan_path is None
    assert any("crop" in note for note in notes), notes


def test_on_keeps_a_plan_that_removes_little_shake(tmp_path: Path) -> None:
    plan_path, _ = _decide(1.0, "on", tmp_path)
    assert plan_path is not None


def test_auto_skips_a_camera_that_barely_moves(tmp_path: Path) -> None:
    plan_path, notes = _decide(0.0001, "auto", tmp_path)
    assert plan_path is None
    assert any("barely moves" in note for note in notes), notes


def test_no_crop_suggestion_leaves_the_measurement_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis.report import SdrLight, build_report
    from mcrawsome.analysis.resolve import Resolution
    from mcrawsome.motion.flow import MotionTrack

    rng = np.random.default_rng(2)
    t = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(t, rng.normal(0, 0.05, (len(t), 3)), None, None, "XYZ", clock="video")
    frames = 0.2 + np.arange(96) / 24.0
    track = MotionTrack(np.arange(95), rng.normal(0, 3.0, 95), rng.normal(0, 3.0, 95), np.zeros(95))
    monkeypatch.setattr("mcrawsome.pipeline.stabilise.shake_removed_by_plan", lambda _plan, _track: 0.1)
    outcome = _finish(
        _fit(), log, frames, 0.02, 0.018, Recipe(), 960, 540, 24.0, CameraProfile(name="p"), tmp_path, False,
        track=track,
    )  # fmt: skip
    assert outcome.plan_path is None
    assert any("remove only 10%" in note for note in outcome.notes)
    assert "suggested_crop" not in outcome.measurements
    report = build_report(Resolution(Recipe()), 0.0, SdrLight(0.4, 0.8), outcome)
    rows = dict(report.sections)["Stabilisation"]
    assert rows == [("Stabilised", "no"), ("Shake removed", "10%")]


def test_a_saved_outcome_is_loaded_with_its_plan(tmp_path: Path) -> None:
    from mcrawsome.pipeline.stabilise import MotionOutcome, load_outcome, save_outcome

    log = GyroLog(np.arange(0.0, 5.0, 1 / 400), np.zeros((2000, 3)), None, None, "XYZ", clock="video")
    frames = 0.2 + np.arange(96) / 24.0
    first = tmp_path / "first"
    first.mkdir()
    outcome = _finish(
        _fit(), log, frames, 0.02, 0.018, Recipe(), 960, 540, 24.0, CameraProfile(name="p"), first, False
    )
    assert outcome.plan_path is not None
    cache = tmp_path / "cache" / "key"
    assert load_outcome(cache, tmp_path) is None
    save_outcome(outcome, {"MINF": "x"}, cache)
    staging = tmp_path / "second"
    staging.mkdir()
    found = load_outcome(cache, staging)
    assert found is not None
    loaded, device = found
    assert device == {"MINF": "x"}
    assert loaded.plan_path is not None
    assert loaded.plan_path.parent == staging
    assert loaded.plan_path.read_bytes() == outcome.plan_path.read_bytes()
    assert (loaded.notes, loaded.measurements) == (outcome.notes, outcome.measurements)

    skipped = MotionOutcome(None, ["not stabilised: no gyro log"])
    save_outcome(skipped, {}, tmp_path / "cache" / "other")
    again = load_outcome(tmp_path / "cache" / "other", staging)
    assert again is not None
    assert again[0].plan_path is None
    assert again[0].notes == skipped.notes


def test_frame_global_planning_keeps_readout_in_exposure_centre_but_has_no_row_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.motion.plan import StabilisationPlan, plan_stabilisation

    seen: list[tuple[Any, ...]] = []

    def capture(*args: Any, **kwargs: Any) -> StabilisationPlan:
        seen.append(args)
        return plan_stabilisation(*args, **kwargs)

    monkeypatch.setattr("mcrawsome.pipeline.stabilise.plan_stabilisation", capture)
    times = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(times, np.random.default_rng(5).normal(0, 0.05, (len(times), 3)), None, None, "XYZ")
    frame_times = 0.2 + np.arange(40) / 24
    exposures = np.linspace(0.005, 0.02, len(frame_times))
    recipe = Recipe.model_validate({"motion": {"stabilise": "on", "bands": 36}})
    outcome = _finish(
        _fit(),
        log,
        frame_times,
        0.005,
        0.018,
        recipe,
        960,
        540,
        24.0,
        CameraProfile(name="test"),
        tmp_path,
        True,
        frame_global=True,
        exposures=exposures,
    )
    np.testing.assert_allclose(seen[0][1], frame_times + exposures / 2 + 0.009 + _fit().offset_s)
    assert seen[0][2] == 0.0
    assert seen[0][6] == 1
    assert outcome.plan_path is not None
    p = StabilisationPlan.load(outcome.plan_path)
    assert p.rotations.shape == (40, 1, 3, 3)
    assert p.rotate_180


def test_native_row_planning_keeps_the_readout_and_asks_for_the_row_bands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.motion.plan import StabilisationPlan, plan_stabilisation

    seen: list[tuple[Any, ...]] = []

    def capture(*args: Any, **kwargs: Any) -> StabilisationPlan:
        seen.append(args)
        return plan_stabilisation(*args, **kwargs)

    monkeypatch.setattr("mcrawsome.pipeline.stabilise.plan_stabilisation", capture)
    times = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(times, np.random.default_rng(5).normal(0, 0.05, (len(times), 3)), None, None, "XYZ")
    frame_times = 0.2 + np.arange(40) / 24
    recipe = Recipe.model_validate({"motion": {"stabilise": "on", "bands": 12}})
    outcome = _finish(
        _fit(), log, frame_times, 0.005, 0.018, recipe, 960, 540, 24.0, CameraProfile(name="test"), tmp_path,
        True, frame_global=True, exposures=np.full(len(frame_times), 0.005), row_bands=12,
    )  # fmt: skip
    assert seen[0][2] == 0.018  # the readout spreads the rows in time
    assert seen[0][6] == 12
    assert outcome.plan_path is not None
    plan = StabilisationPlan.load(outcome.plan_path)
    assert plan.rotations.shape == (40, 12, 3, 3)
    assert not np.allclose(plan.rotations[:, 0], plan.rotations[:, 11])  # rows differ under motion


@pytest.mark.parametrize(("known", "spread"), [(True, 0.018 * 937.5 / 750.0), (False, 0.018)])
def test_native_rows_are_spread_for_the_lens_focal_length_when_it_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, known: bool, spread: float
) -> None:
    """Resolve projects every pose with the fitted focal length; a row's share of the frame's turn is
    spread wider (or narrower) so the rows correct as the lens's own focal length would."""
    from mcrawsome.motion.plan import StabilisationPlan, plan_stabilisation

    seen: list[tuple[Any, ...]] = []

    def capture(*args: Any, **kwargs: Any) -> StabilisationPlan:
        seen.append(args)
        return plan_stabilisation(*args, **kwargs)

    monkeypatch.setattr("mcrawsome.pipeline.stabilise.plan_stabilisation", capture)
    times = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(times, np.random.default_rng(5).normal(0, 0.05, (len(times), 3)), None, None, "XYZ")
    frame_times = 0.2 + np.arange(40) / 24
    recipe = Recipe.model_validate({"motion": {"stabilise": "on", "bands": 12}})
    lens = CameraProfile(name="test", focal_px=937.5)  # the fit (750 px) settled 20% below the lens
    outcome = _finish(
        _fit(), log, frame_times, 0.005, 0.018, recipe, 960, 540, 24.0, lens, tmp_path,
        True, frame_global=True, exposures=np.full(len(frame_times), 0.005), row_bands=12, focal_known=known,
    )  # fmt: skip
    assert seen[0][2] == pytest.approx(spread)
    np.testing.assert_allclose(seen[0][1], frame_times + 0.0025 + 0.009)  # exposure centres keep the readout
    assert seen[0][10] == 750.0  # frame poses and Resolve's projection keep the fitted focal length
    assert outcome.measurements["row_focal_px"] == (937.5 if known else 750.0)


def test_a_refused_sync_is_checked_again_where_the_camera_turns_most(monkeypatch: pytest.MonkeyPatch) -> None:
    """Walking moves the picture in ways the gyro cannot explain; the five seconds inside the sync window
    where the camera turns most can confirm the timing on their own."""
    from dataclasses import replace

    import mcrawsome.pipeline.stabilise as module
    from mcrawsome.motion.flow import MotionTrack

    t = np.arange(0.0, 60.0, 1 / 400)
    gyro = np.zeros((len(t), 3))
    gyro[(t > 42) & (t < 46), 0] = 0.4
    log = GyroLog(t, gyro, None, None, "XYZ", clock="video")
    times = np.arange(1440) / 24.0
    measured: list[list[int]] = []
    fitted: list[tuple[int, int]] = []

    def measure(indices: list[int]) -> MotionTrack:
        measured.append(indices)
        first = np.asarray(indices[:-1], dtype=np.int64)
        return MotionTrack(first, np.zeros(len(first)), np.zeros(len(first)), np.zeros(len(first)))

    def fit(log: GyroLog, times: np.ndarray, exposure: float, track: MotionTrack, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        fitted.append((int(track.first[0]), int(track.first[-1])))
        refused = len(fitted) == 1
        return replace(_fit(), reason="gyro and picture do not agree well enough" if refused else None)

    monkeypatch.setattr(module, "fit_sync", fit)
    recipe = Recipe.model_validate({"motion": {"stabilise": "on"}})
    result, track, frames = module._synced(
        log, times, 0.02, measure, CameraProfile(name="test"), "gcsv", 0.018, recipe, 24.0, False
    )
    assert len(measured) == 1  # the short check reuses the frames already measured
    window = measured[0]
    assert len(window) == 480
    assert window[0] <= 42 * 24
    assert window[-1] >= 46 * 24
    assert len(fitted) == 2
    first, last = fitted[1]
    assert last - first == 118  # five seconds of frame pairs
    assert first <= 42 * 24  # holding the whole pan
    assert last >= 46 * 24 - 1
    assert result.reason is None
    assert frames == (first, last + 1)
    assert result.note is not None
    assert f"frames {first} to {last + 1}" in result.note
    assert len(track.first) == len(window) - 1  # the plan's own check still uses the whole window


def _recorded_clip(make_mcraw: Any, path: Path, ois_px: float = 2.0) -> Path:
    from conftest import RECORDED_CONTAINER, RECORDED_FRAME, SYNTHETIC_STEP, recorded_motion

    stamps = [1_000_000_000 + i * SYNTHETIC_STEP for i in range(48)]
    return make_mcraw(  # type: ignore[no-any-return]
        path,
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], 2.0, ois_px=ois_px),
    )


def test_a_clip_with_recorded_motion_is_planned_on_its_own_clock_and_calibration(
    make_mcraw: Any, tmp_path: Path
) -> None:
    from mcrawsome.pipeline.stabilise import plan_raw_clip

    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    assert not path.with_suffix(".gcsv").exists()
    recipe = Recipe.model_validate({"motion": {"stabilise": "on", "optical_correction": False}})
    told: list[str] = []
    outcome = plan_raw_clip(path, recipe, 1, tmp_path, stage=told.append)
    assert outcome.plan_path is not None, outcome.notes
    assert told == [
        "Reading its gyro",
        "Measuring the picture to check the gyro",
        "Planning the stabilisation",
    ]
    measured = outcome.measurements
    assert measured["gyro_source"] == "mcraw"
    assert measured["lens_shift_compensated"] is True
    assert measured["sync_offset_ms"] == 0
    assert measured["focal_px"] == 100.0  # the recorded calibration, not a guess from the width
    assert measured["readout_ms"] == pytest.approx(8 * 64 / 96, abs=0.01)  # the recorded rows' share
    assert measured["gyro_axes"] == [0, 1, 2]
    assert measured["gyro_signs"] == [1, -1, 1]


def test_the_recorded_calibration_takes_the_place_of_a_matching_profile(
    make_mcraw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.camera import profile as profiles
    from mcrawsome.camera.profile import Axes
    from mcrawsome.io.clip import Clip
    from mcrawsome.pipeline.stabilise import camera_profile, focal_is_known

    measured = CameraProfile(name="checkerboard", focal_px=150.0, readout_ms=18.0)
    monkeypatch.setattr(profiles, "find_profile", lambda *_: measured)
    monkeypatch.setattr("mcrawsome.pipeline.stabilise.find_profile", lambda *_: measured)
    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    with Clip(path) as clip:
        recorded = camera_profile(clip, Recipe())
        overridden = camera_profile(
            clip, Recipe.model_validate({"camera": {"focal_px": 120.0, "readout_ms": 3.0}})
        )
        assert focal_is_known(clip, Recipe())
    assert recorded.focal_px == 100.0
    assert recorded.readout_ms == pytest.approx(8 * 64 / 96)
    assert recorded.mcraw_axes == Axes(axes=(0, 1, 2), signs=(1, -1, 1))
    assert recorded.gcsv_gravity_axes == Axes(axes=(1, 0, 2), signs=(-1, -1, -1))
    assert recorded.lens_calibration is not None
    assert recorded.lens_calibration.coefficients == (0.05, -0.1, 0.0, 0.0, 0.07)
    assert "lens calibration recorded in the clip" in recorded.description
    assert overridden.focal_px == 120.0  # a Recipe override still wins
    assert overridden.readout_ms == 3.0


def _mode_finish(mode: str, tmp_path: Path, **motion: Any) -> Any:
    from mcrawsome.motion.flow import MotionTrack

    rng = np.random.default_rng(2)
    t = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(t, rng.normal(0, 0.05, (len(t), 3)), None, None, "XYZ", clock="video")
    frames = 0.2 + np.arange(96) / 24.0
    track = MotionTrack(np.arange(95), rng.normal(0, 3.0, 95), rng.normal(0, 3.0, 95), np.zeros(95))
    recipe = Recipe.model_validate({"motion": {"stabilise": "auto", "mode": mode, **motion}})
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _finish(
        _fit(), log, frames, 0.02, 0.018, recipe, 960, 540, 24.0, CameraProfile(name="p"), tmp_path, False,
        track=track, frame_global=True, row_bands=12,
    )  # fmt: skip


@pytest.mark.parametrize("mode", ["rolling_shutter", "tripod", "pan"])
def test_a_mode_asked_for_by_name_is_planned_even_when_little_shake_goes(mode: str, tmp_path: Path) -> None:
    smooth = _mode_finish("smooth", tmp_path / "smooth")  # the same picture: auto judges it not worth it
    assert smooth.plan_path is None
    outcome = _mode_finish(mode, tmp_path / mode)
    assert outcome.plan_path is not None, outcome.notes
    assert outcome.measurements["mode"] == mode


def test_correcting_only_the_rolling_shutter_needs_its_compensation_on(tmp_path: Path) -> None:
    from mcrawsome.pipeline.stabilise import StabiliseError

    recipe = Recipe.model_validate({"motion": {"stabilise": "on", "mode": "rolling_shutter"}})
    t = np.arange(0.0, 5.0, 1 / 400)
    log = GyroLog(t, np.zeros((len(t), 3)), None, None, "XYZ", clock="video")
    with pytest.raises(StabiliseError, match="Rolling shutter compensation on"):
        _finish(
            _fit(), log, 0.2 + np.arange(96) / 24.0, 0.02, 0.018, recipe, 960, 540, 24.0,
            CameraProfile(name="p"), tmp_path, False, frame_global=True, row_bands=0,
        )  # fmt: skip


def test_horizon_lock_is_left_out_when_only_the_rolling_shutter_is_corrected(tmp_path: Path) -> None:
    outcome = _mode_finish("rolling_shutter", tmp_path, horizon_lock=True)  # no gravity axes: not needed
    assert outcome.plan_path is not None
    assert "horizon_lock" not in outcome.measurements
    assert any("Horizon Lock was not applied" in note for note in outcome.notes)
