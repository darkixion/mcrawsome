from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.orientation import AxisMapping, Orientation

DATA = Path(__file__).parent.parent / "data"


def _log(times: np.ndarray, gyro: np.ndarray) -> GyroLog:
    return GyroLog(
        times_s=times, gyro=gyro, accel=None, start_epoch_s=None, orientation="", header={}, clock="video"
    )


def test_orientation_matches_prototype() -> None:
    ref = np.load(DATA / "ref_motion.npz")
    orient = Orientation.from_gyro(_log(ref["times"], ref["gyro"]), AxisMapping((0, 1, 2), (1, -1, 1)))
    np.testing.assert_allclose(orient.at(ref["frame_times"]), ref["q"], atol=1e-9)


def test_coverage_mask() -> None:
    times = np.arange(1.0, 2.0, 0.01)
    orient = Orientation.from_gyro(_log(times, np.zeros((len(times), 3))), AxisMapping((0, 1, 2), (1, 1, 1)))
    assert orient.covers(np.array([0.5, 1.5, 2.5])).tolist() == [False, True, False]


def test_a_hole_in_the_log_is_not_covered() -> None:
    times = np.arange(0.0, 10.0, 0.0025)
    times = times[(times < 4.0) | (times > 5.5)]
    orient = Orientation.from_gyro(_log(times, np.zeros((len(times), 3))), AxisMapping((0, 1, 2), (1, 1, 1)))
    assert orient.covers(np.array([3.9, 4.5, 5.0, 5.6])).tolist() == [True, False, False, True]


@pytest.mark.parametrize("pitch", [False, True])
def test_long_integration_keeps_the_normalised_recurrence_without_per_sample_array_dispatch(
    monkeypatch: pytest.MonkeyPatch, pitch: bool
) -> None:
    from mcrawsome.motion.geometry import quat_from_rotvec, quat_mul

    times = np.arange(8001) / 400
    gyro = np.random.default_rng(19).normal(0, 3.0, (len(times), 3))
    mapping, gains = AxisMapping((2, 0, 1), (-1, 1, -1)), (0.4, 0.8, 1.0)
    w = np.stack([mapping.signs[k] * gyro[:, mapping.axes[k]] * gains[k] for k in range(3)], -1)
    rate = np.stack([-w[:, 1], w[:, 0], w[:, 2]], -1)
    if not pitch:
        rate[:, 0] = 0
    steps = quat_from_rotvec(0.5 * (rate[1:] + rate[:-1]) * np.diff(times)[:, None])
    expected = np.zeros((len(times), 4))
    expected[0, 0] = 1
    for i, step in enumerate(steps, 1):
        q = quat_mul(step, expected[i - 1])
        expected[i] = q / np.linalg.norm(q)

    moveaxis = np.moveaxis
    calls = 0

    def counted(a: np.ndarray, source: int, destination: int) -> np.ndarray:
        nonlocal calls
        calls += 1
        return moveaxis(a, source, destination)

    monkeypatch.setattr(np, "moveaxis", counted)
    actual = Orientation.from_gyro(_log(times, gyro), mapping, pitch, gains)
    np.testing.assert_allclose(actual.quats, expected, rtol=0, atol=2e-13)
    np.testing.assert_allclose(np.linalg.norm(actual.quats, axis=1), 1, rtol=0, atol=4e-16)
    assert calls <= 2
