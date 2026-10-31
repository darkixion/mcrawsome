import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.io.gyro import GyroLog


def _log(start: float, end: float) -> GyroLog:
    times = np.linspace(start, end, 200)
    return GyroLog(times_s=times, gyro=np.zeros((200, 3)), accel=None, start_epoch_s=None, orientation="XYZ")


def test_full_partial_and_no_coverage() -> None:
    log = _log(1.0, 3.0)
    assert log.coverage(1.5, 2.5) == 1.0
    assert log.coverage(0.0, 2.0) == 0.5
    assert log.coverage(4.0, 5.0) == 0.0


@given(st.floats(-5, 5), st.floats(0.01, 10), st.floats(-5, 5), st.floats(0.01, 10))
def test_coverage_is_a_fraction(a: float, la: float, b: float, lb: float) -> None:
    assert 0.0 <= _log(a, a + la).coverage(b, b + lb) <= 1.0


def _log_with_hole(hole: tuple[float, float]) -> GyroLog:
    times = np.arange(0.0, 10.0, 0.0025)
    times = times[(times < hole[0]) | (times > hole[1])]
    return GyroLog(times, np.zeros((len(times), 3)), None, None, "XYZ")


def test_a_hole_in_the_log_is_not_covered() -> None:
    log = _log_with_hole((4.0, 5.5))
    assert log.coverage(0.0, 10.0) == pytest.approx(0.85, abs=0.01)
    assert log.coverage(4.2, 5.2) == pytest.approx(0.0, abs=0.01)


def test_short_gaps_are_bridged() -> None:
    # Direct Log telemetry leaves one-frame gaps where blocks are empty: those are not holes
    log = _log_with_hole((4.0, 4.05))
    assert log.coverage(0.0, 10.0) == pytest.approx(1.0, abs=0.001)
