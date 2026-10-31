from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.flow import MotionTrack, Strips
from mcrawsome.motion.sync import cumulative_rotation
from mcrawsome.pipeline.calibrate import Recording, calibrate

FPS, FOCAL, OFFSET, READOUT = 24.0, 3000.0, -0.104, 0.018


def _log(seconds: float = 30.0, axes_moving: tuple[float, float, float] = (1.0, 1.0, 1.0)) -> GyroLog:
    rng = np.random.default_rng(4)
    t = np.arange(0, seconds, 1 / 400)
    kernel = np.exp(-0.5 * (np.arange(-60, 61) / 20) ** 2)
    raw = rng.standard_normal((len(t), 3))
    smooth = np.stack([np.convolve(raw[:, k], kernel / kernel.sum(), "same") for k in range(3)], -1)
    return GyroLog(t, smooth * 1.5 * np.array(axes_moving), None, None, "XYZ", clock="video")


def _recording(log: GyroLog, image_axes: tuple[int, int, int] = (0, 1, 2)) -> Recording:
    frames = 2.0 + np.arange(600) / FPS
    exposure = 0.5 / FPS
    cum = cumulative_rotation(log)
    rng = np.random.default_rng(6)

    def track(rows: tuple[float, float]) -> MotionTrack:
        centre = frames + exposure / 2 + READOUT * sum(rows) / 2 + OFFSET
        rot = np.stack(
            [np.interp(centre[1:], log.times_s, cum[:, k]) - np.interp(centre[:-1], log.times_s, cum[:, k])
             for k in range(3)], -1,
        )  # fmt: skip
        n = len(rot)
        return MotionTrack(
            np.arange(n),
            FOCAL * rot[:, image_axes[0]] + rng.normal(0, 0.5, n),
            -FOCAL * rot[:, image_axes[1]] + rng.normal(0, 0.5, n),
            rot[:, image_axes[2]],
        )

    def measure(strips: Strips) -> list[MotionTrack]:
        return [track(rows) for rows in strips]

    return Recording(
        Path("calib.mcraw"), "gpmf", "pixel test", None, 3840, 2160, FPS, log, frames, exposure, measure
    )


def test_calibration_recovers_the_camera() -> None:
    result = calibrate(_recording(_log()), name="test_camera")
    profile = result.profile
    assert result.fit.reason is None
    assert profile.name == "test_camera"
    assert profile.focal_px == pytest.approx(FOCAL, rel=0.03)
    assert profile.readout_ms == pytest.approx(READOUT * 1000, abs=2.0)
    assert profile.gpmf_axes is not None
    assert profile.gpmf_axes.axes == (0, 1, 2)
    assert profile.gpmf_axes.signs == (1, -1, 1)
    assert profile.gpmf_sync_ms == pytest.approx(OFFSET * 1000, abs=2.0)
    assert profile.match.width == 3840
    assert profile.match.device_model == "pixel test"


def test_a_recording_that_cannot_tell_the_axes_apart_is_not_trusted() -> None:
    # both image directions follow the same gyro axis, as when a recording only ever pans one way
    result = calibrate(_recording(_log(), image_axes=(0, 0, 2)))
    assert result.fit.reason is not None
    assert "axes" in result.fit.reason
