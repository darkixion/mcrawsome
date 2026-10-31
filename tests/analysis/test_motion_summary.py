"""Movement, shake blur and trim suggestions from synthetic gyro logs with known answers."""

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import pytest

from mcrawsome.analysis.motion_summary import (
    BLURRED_PX,
    blurred_stretches,
    frame_blur,
    movement,
    rates,
    steadiest_frame,
    suggest_trim,
)
from mcrawsome.io.gyro import GyroLog

RATE = 400.0
GRAVITY = 9.80665
Array = npt.NDArray[np.float64]
Signal = Callable[[Array], Array]


def log(
    seconds: float, gyro: Signal | None = None, vertical: Signal | None = None, times: Array | None = None
) -> GyroLog:
    t = np.arange(0, seconds, 1 / RATE) if times is None else times
    rotation = np.zeros((len(t), 3)) if gyro is None else gyro(t)
    accel = np.zeros((len(t), 3))
    accel[:, 1] = GRAVITY + (vertical(t) if vertical is not None else 0.0)
    return GyroLog(times_s=t, gyro=rotation, accel=accel, start_epoch_s=0.0, orientation="XYZ")


def turning(rate_deg_s: float) -> Signal:
    return lambda t: np.stack(
        [np.zeros_like(t), np.full_like(t, np.radians(rate_deg_s)), np.zeros_like(t)], -1
    )


def shaking(amplitude_deg_s: float, hertz: float = 8.0) -> Signal:
    return lambda t: np.stack(
        [np.radians(amplitude_deg_s) * np.sin(2 * np.pi * hertz * t), np.zeros_like(t), np.zeros_like(t)], -1
    )


def test_rates_refuse_logs_that_cannot_be_timed() -> None:
    broken = log(5)
    with pytest.raises(ValueError, match="increasing"):
        rates(GyroLog(broken.times_s[::-1].copy(), broken.gyro, None, 0.0, "XYZ"), 0, 5)
    with pytest.raises(ValueError, match="cover"):
        rates(log(5), 20, 30)


def test_a_steady_pan_is_intended_movement_not_shake() -> None:
    r = rates(log(10, turning(20)), 1, 9)
    inner = (r.times > 2) & (r.times < 8)
    assert np.allclose(r.total[inner], 20, atol=1e-6)
    assert r.shake[inner].max() < 1e-6
    centres = np.arange(2, 8, 1 / 24)
    assert np.nanmax(frame_blur(r, centres, 1 / 48, 2700)) < 1e-3


def test_shake_blur_is_the_shake_swept_during_each_exposure() -> None:
    r = rates(log(10, shaking(30)), 1, 9)
    centres = np.arange(2, 8, 1 / 24)
    blur = frame_blur(r, centres, 1 / 48, 2700)
    # The mean of |sin| is 2/pi: 30 degrees/s of 8 Hz shake sweeps about 0.27 degrees in 1/48 s.
    expected = np.radians(30) * 2 / np.pi / 48 * 2700
    assert np.nanmean(blur) == pytest.approx(expected, rel=0.08)
    longer = frame_blur(r, centres, 1 / 24, 2700)
    assert np.nanmean(longer) == pytest.approx(2 * np.nanmean(blur), rel=0.05)


def test_frames_the_log_does_not_cover_have_no_blur() -> None:
    times = np.r_[np.arange(0, 4, 1 / RATE), np.arange(5, 10, 1 / RATE)]
    r = rates(log(10, shaking(30), times=times), 0, 10)
    blur = frame_blur(r, np.array([2.0, 4.5, 7.0, 40.0]), 1 / 48, 2700)
    assert np.isfinite(blur[[0, 2]]).all()
    assert np.isnan(blur[[1, 3]]).all()


@pytest.mark.parametrize(
    ("gyro", "vertical", "expected"),
    [
        (None, None, "still"),
        (turning(15), None, "pan"),
        (shaking(3, 2.0), lambda t: 1.2 * np.sin(2 * np.pi * 1.8 * t), "moving"),
        (shaking(12, 3.0), None, "handheld"),
    ],
)
def test_each_second_has_one_movement(gyro: Signal | None, vertical: Signal | None, expected: str) -> None:
    summary = movement(rates(log(12, gyro, vertical), 1, 11), 1, 11)
    assert summary.movement == expected
    assert summary.shares[expected] == 1.0
    assert sum(summary.shares.values()) == pytest.approx(1.0)


def test_a_clip_shorter_than_a_second_has_no_movement() -> None:
    assert movement(rates(log(3), 1, 1.4), 1, 1.4).movement is None


def jolted(at: list[float]) -> Signal:
    def gyro(t: Array) -> Array:
        rotation = shaking(5, 3.0)(t)
        for centre in at:
            rotation[:, 2] += np.radians(80) * np.exp(-(((t - centre) / 0.08) ** 2))
        return rotation

    return gyro


def test_trims_leave_out_the_jolts_of_pressing_record_and_stop() -> None:
    r = rates(log(22, jolted([1.3, 20.5])), 1, 21)
    trim = suggest_trim(r, 1.0, 20.0)
    assert 0.4 < trim.start_s < 0.8
    assert 19.0 < trim.end_s < 19.5
    assert [reason.split(":")[0] for reason in trim.reasons] == ["Start", "End"]
    assert "pressing stop" in trim.reasons[1]


def test_ordinary_movement_near_the_ends_keeps_the_whole_clip() -> None:
    r = rates(log(22, shaking(10, 3.0)), 1, 21)
    trim = suggest_trim(r, 1.0, 20.0)
    assert (trim.start_s, trim.end_s, trim.reasons) == (0.0, 20.0, [])


def test_focus_settling_moves_the_mark_in() -> None:
    r = rates(log(22, shaking(5, 3.0)), 1, 21)
    focus = np.full(480, 2.0)
    focus[:12] = np.linspace(4.0, 2.0, 12)
    trim = suggest_trim(r, 1.0, 20.0, focus, 24.0)
    assert trim.start_s == pytest.approx(11 / 24, abs=1 / 24)
    assert trim.reasons == [f"Start: focus is still moving until {trim.start_s:.1f} s"]


def test_trims_never_take_more_than_two_seconds_or_half_the_clip() -> None:
    r = rates(log(22, jolted([1.2, 2.4, 20.5])), 1, 21)
    assert suggest_trim(r, 1.0, 20.0).start_s <= 2.0
    # A 4.5 s clip jolted near both ends would keep less than half: it stays whole.
    short = rates(log(8, jolted([2.3, 4.2])), 1, 5.5)
    assert suggest_trim(short, 1.0, 4.5) == suggest_trim(rates(log(8), 1, 5.5), 1.0, 4.5)
    assert suggest_trim(short, 1.0, 4.5).reasons == []


def test_the_steadiest_frame_comes_from_the_middle_half_and_avoids_focus_changes() -> None:
    blur = np.full(100, 10.0)
    blur[5] = 0.0  # outside the middle half
    blur[40] = 1.0
    blur[60] = 2.0
    assert steadiest_frame(blur) == 40
    focus = np.full(100, 1.0)
    focus[38:43] = [1.0, 1.2, 1.5, 1.8, 2.0]
    assert steadiest_frame(blur, focus) == 60
    assert steadiest_frame(np.full(10, np.nan)) is None


def test_blurred_stretches_need_sustained_blur() -> None:
    blur = np.zeros(96)
    blur[10:14] = BLURRED_PX + 5  # a sixth of a second at 24 fps: too brief
    blur[40:70] = BLURRED_PX + 1
    blur[55] = 90
    assert blurred_stretches(blur, 24.0) == [(40, 30, 90.0)]
