"""A RAW clip's gyro: motion recorded inside the .mcraw first, else the .gcsv beside it."""

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from conftest import RECORDED_CONTAINER, RECORDED_FRAME, SYNTHETIC_STEP, recorded_motion

from mcrawsome.camera.profile import Axes, CameraProfile
from mcrawsome.io.clip import Clip
from mcrawsome.io.gyro import GyroLog
from mcrawsome.io.recorded_motion import Samples
from mcrawsome.motion.raw_gyro import has_gyro, has_recorded_motion, raw_gyro, with_lens_shift
from mcrawsome.motion.sync import cumulative_rotation


def _recorded(make_mcraw: Callable[..., Path], path: Path, frames: int = 24, ois_px: float = 0.0) -> Path:
    stamps = [5_000_000_000 + i * SYNTHETIC_STEP for i in range(frames)]
    return make_mcraw(
        path,
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], frames / 24, ois_px=ois_px),
    )


def test_recorded_motion_runs_on_the_frames_clock_with_its_calibration(
    make_mcraw: Callable[..., Path], make_gcsv: Callable[..., Path], tmp_path: Path
) -> None:
    path = _recorded(make_mcraw, tmp_path / "clip.mcraw", ois_px=2.0)
    make_gcsv(path.with_suffix(".gcsv"), 24)  # a log beside it as well: the recording's own wins
    with Clip(path) as clip:
        source = raw_gyro(clip)
    assert source is not None
    assert source.recorded
    assert source.log.clock == "video"
    assert source.log.times_s[0] == pytest.approx(-0.5)  # half a second before the first frame
    assert source.log.accel is not None
    assert source.log.accel.shape == source.log.gyro.shape
    assert np.allclose(source.log.accel[:, 0], 9.80665)
    camera = source.camera
    assert camera is not None
    first_row = 0.008 * 16 / 96  # the 64 recorded rows start 16 rows into the 96-row array
    assert source.frame_times_s[0] == pytest.approx(first_row)
    assert np.allclose(np.diff(source.frame_times_s), SYNTHETIC_STEP / 1e9)
    assert source.ois is not None
    assert source.ois.times_ns[0] == -500_000_000
    assert source.sync_s(CameraProfile(name="p", gcsv_sync_ms=-105)) == 0  # no offset on its own clock


def test_a_gcsv_is_used_when_nothing_is_recorded_in_the_clip(
    make_mcraw: Callable[..., Path], make_gcsv: Callable[..., Path], tmp_path: Path
) -> None:
    path = make_mcraw(tmp_path / "old.mcraw", [i * SYNTHETIC_STEP for i in range(24)])
    assert not has_gyro(path)
    make_gcsv(path.with_suffix(".gcsv"), 24)
    assert has_gyro(path)
    assert not has_recorded_motion(path)
    with Clip(path) as clip:
        source = raw_gyro(clip)
        linked = raw_gyro(clip, link_frames=4)
    assert source is not None
    assert linked is not None
    assert source.source == "gcsv"
    assert source.camera is None
    assert source.ois is None
    # Fewer receive times only anchor the frames less exactly; their spacing is the recorded one.
    assert np.allclose(np.diff(linked.frame_times_s), np.diff(source.frame_times_s))
    assert source.sync_s(CameraProfile(name="p", gcsv_sync_ms=-105)) == pytest.approx(-0.105)


def test_lens_shifts_join_the_gyro_as_the_turn_that_moves_the_picture_as_far() -> None:
    # A still camera whose lens moves the picture 6 px right and 3 px down over a second: as a turn, the
    # mapped rotation (picture x, y) must come out at 6 / 600 and 3 / 600 radians.
    t = np.arange(0.0, 1.0, 1 / 400)
    log = GyroLog(t, np.zeros((len(t), 3)), None, None, "XYZ", clock="video")
    times = np.arange(0, 1_000_000_001, 5_000_000, dtype=np.int64)
    shift = np.stack([6.0 * times / 1e9, 3.0 * times / 1e9], -1)
    axes = Axes(axes=(0, 1, 2), signs=(1, -1, 1))
    shifted = with_lens_shift(log, Samples(times, shift), axes, 600.0, 1.0)
    turned = cumulative_rotation(shifted)[-1] - cumulative_rotation(shifted)[0]
    mapped = [axes.signs[k] * turned[axes.axes[k]] for k in range(3)]
    assert mapped == pytest.approx([6 / 600 * t[-1], 3 / 600 * t[-1], 0.0], abs=1e-6)
    halved = with_lens_shift(log, Samples(times, shift), axes, 600.0, 0.5)  # a binned frame
    half = cumulative_rotation(halved)[-1]
    assert half[0] == pytest.approx(turned[0] / 2, abs=1e-6)


def test_the_picture_log_folds_in_lens_shifts_only_with_the_recorded_axes(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    path = _recorded(make_mcraw, tmp_path / "clip.mcraw", ois_px=2.0)
    with Clip(path) as clip:
        source = raw_gyro(clip)
    assert source is not None
    profile = CameraProfile(name="p", focal_px=100.0, mcraw_axes=Axes(axes=(0, 1, 2), signs=(1, -1, 1)))
    picture, shifted = source.picture_log(profile)
    assert shifted
    assert not np.allclose(picture.gyro[:, 0], source.log.gyro[:, 0])
    assert np.allclose(picture.gyro[:, 2], source.log.gyro[:, 2])  # lens shifts never roll the picture
    unchanged, applied = source.picture_log(profile.model_copy(update={"mcraw_axes": None}))
    assert not applied
    assert unchanged is source.log
