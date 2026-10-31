from dataclasses import replace

import numpy as np
import pytest

from mcrawsome.analysis.motion_markers import suggest
from mcrawsome.io.gyro import GyroLog


def recording() -> GyroLog:
    t = np.arange(0, 9, 0.005)
    gyro = np.zeros((len(t), 3))
    gyro[(t >= 3) & (t < 6), 1] = 0.2
    shake = t >= 6
    gyro[shake, 0] = 1.5 * np.sin(2 * np.pi * 8 * t[shake])
    accel = np.tile([0, -9.80665, 0], (len(t), 1))
    return GyroLog(t, gyro, accel, None, "XYZ")


def test_marks_sustained_low_motion_pan_and_shake_as_suggestions() -> None:
    markers = suggest(recording(), np.arange(216) / 24, 24)
    assert [(m.name, m.colour) for m in markers] == [
        ("Low motion", "Green"),
        ("Steady rotation", "Blue"),
        ("Possible shake", "Red"),
    ]
    assert [m.frame for m in markers] == [0, 72, 144]
    assert all("suggestion" in m.note for m in markers)


def test_rotating_gravity_is_not_a_camera_bump() -> None:
    log = recording()
    t = log.times_s
    gyro = np.tile([0.2, 0, 0], (len(t), 1))
    accel = np.stack([0 * t, -9.80665 * np.cos(0.2 * t), -9.80665 * np.sin(0.2 * t)], -1)
    markers = suggest(replace(log, gyro=gyro, accel=accel), np.arange(216) / 24, 24)
    assert len(markers) == 1
    assert markers[0].name == "Steady rotation"


def test_acceleration_spike_can_flag_shake_without_angular_rotation() -> None:
    log = recording()
    assert log.accel is not None
    log.accel[(log.times_s > 4) & (log.times_s < 5), 1] *= 2.0
    markers = suggest(replace(log, gyro=np.zeros_like(log.gyro)), np.arange(216) / 24, 24)
    assert any(m.name == "Possible shake" and m.frame <= 108 and m.frame + m.duration >= 100 for m in markers)


def test_trim_and_repetitions_are_in_the_output_marker_timebase() -> None:
    log = recording()
    centres = np.repeat(np.arange(72, 144) / 24, 2)
    [marker] = suggest(log, centres, 24)
    assert (marker.frame, marker.duration, marker.name) == (0, 144, "Steady rotation")


def test_does_not_mark_sensor_gaps_or_uncovered_frames() -> None:
    log = recording()
    keep = (log.times_s < 1.5) | (log.times_s > 4.5)
    assert log.accel is not None
    log = replace(log, times_s=log.times_s[keep], gyro=log.gyro[keep], accel=log.accel[keep])
    centres = np.arange(-24, 264) / 24
    markers = suggest(log, centres, 24)
    assert all(not (m.frame < 132 and m.frame + m.duration > 60) for m in markers)
    assert all(m.frame >= 24 and m.frame + m.duration <= 240 for m in markers)


def test_requires_acceleration_instead_of_claiming_translation_from_gyro_only() -> None:
    with pytest.raises(ValueError, match="accelerometer"):
        suggest(replace(recording(), accel=None), np.arange(72) / 24, 24)


def test_ordinary_handheld_motion_does_not_trigger_shake() -> None:
    log = recording()
    t = log.times_s
    ordinary = np.stack([0.15 * np.sin(2 * np.pi * 2 * t), 0.1 * np.cos(t), t * 0], -1)
    assert not any(
        marker.name == "Possible shake"
        for marker in suggest(replace(log, gyro=ordinary), np.arange(216) / 24, 24)
    )


def test_moderate_handheld_variation_no_longer_crosses_shake_threshold() -> None:
    log = recording()
    t = log.times_s
    # About35 deg/s spread: used to exceed30, now stays below40.
    movement = np.stack([0.86 * np.sin(2 * np.pi * 8 * t), t * 0, t * 0], -1)
    assert not any(
        marker.name == "Possible shake"
        for marker in suggest(replace(log, gyro=movement), np.arange(216) / 24, 24)
    )
