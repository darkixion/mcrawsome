from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mcrawsome.analysis.gyro_plot import MAX_BUCKETS, envelopes, measure_gyro
from mcrawsome.errors import McError
from mcrawsome.io.gyro import GyroLog


def test_bounded_envelopes_keep_spikes_all_axes_and_convert_units() -> None:
    times = 50 + np.arange(4096) / 400
    gyro = np.zeros((len(times), 3))
    gyro[1000, 0], gyro[1001, 1], gyro[2000, 2] = np.pi, -np.pi / 2, 0.5
    accel = np.tile([0.0, 9.81, 0.0], (len(times), 1))
    plot = envelopes(GyroLog(times, gyro, accel, None, "XYZ"))
    assert plot.acceleration is not None
    assert plot.samples == 4096
    assert len(plot.angular_velocity) == MAX_BUCKETS
    assert plot.sample_rate_hz == pytest.approx(400)
    assert plot.angular_velocity[0].start_s == 0
    assert plot.angular_velocity[-1].end_s == pytest.approx(4095 / 400)
    assert max(b.maximum[0] for b in plot.angular_velocity) == pytest.approx(180)
    assert min(b.minimum[1] for b in plot.angular_velocity) == pytest.approx(-90)
    assert max(b.maximum[2] for b in plot.angular_velocity) == pytest.approx(np.rad2deg(0.5))
    assert plot.acceleration[0].mean == pytest.approx([0, 9.81, 0])


@pytest.mark.parametrize("kind", ["empty", "nan", "backwards"])
def test_unreadable_samples_are_reported(kind: str) -> None:
    times, gyro = np.array([0.0, 0.1]), np.zeros((2, 3))
    if kind == "empty":
        times, gyro = times[:0], gyro[:0]
    elif kind == "nan":
        gyro[0, 0] = np.nan
    else:
        times[1] = -1
    with pytest.raises(ValueError, match="finite with increasing"):
        envelopes(GyroLog(times, gyro, None, None, "XYZ"))


def test_missing_log_is_an_error_and_does_not_read_raw(tmp_path: Path) -> None:
    with pytest.raises(McError, match="No matching"):
        measure_gyro(tmp_path / "missing.mcraw")


def test_hover_frame_links_use_irregular_recorded_times_and_keep_lead_in_unlinked() -> None:
    from mcrawsome.analysis.gyro_plot import GyroBucket, GyroPlot, attach_frames

    groups = [
        GyroBucket(start_s=time, end_s=time, minimum=[0] * 3, maximum=[1] * 3, mean=[0.5] * 3)
        for time in [0.1, 0.24, 0.425, 0.7]
    ]
    report = GyroPlot(
        samples=100,
        duration_s=1,
        sample_rate_hz=100,
        angular_velocity=groups,
        acceleration=[g.model_copy() for g in groups],
    )
    attach_frames(report, np.asarray([0.2, 0.24, 0.5]))
    assert [g.frame for g in report.angular_velocity] == [None, 1, 2, None]
    assert report.acceleration is not None
    assert [g.frame for g in report.acceleration] == [None, 1, 2, None]
    with pytest.raises(ValueError, match="strictly increasing"):
        attach_frames(report, np.asarray([0.1, 0.1]))


def test_unlinkable_raw_clock_preserves_imu_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis import gyro_plot

    path = tmp_path / "unlinked.mcraw"
    path.with_suffix(".gcsv").write_text("gyro")
    log = GyroLog(np.array([0.0, 0.1]), np.zeros((2, 3)), None, None, "XYZ")
    monkeypatch.setattr(gyro_plot, "read_gcsv", lambda *args, **kwargs: log)
    report = measure_gyro(path)
    assert report.samples == 2
    assert report.frame_note is not None
    assert report.frame_note.startswith("Recorded frame link unavailable")
    assert all(group.frame is None for group in report.angular_velocity)


def test_motion_recorded_in_a_clip_is_drawn_with_its_frames(make_mcraw: Any, tmp_path: Path) -> None:
    from conftest import RECORDED_CONTAINER, RECORDED_FRAME, SYNTHETIC_STEP, recorded_motion

    stamps = [1_000_000_000 + i * SYNTHETIC_STEP for i in range(24)]
    path = make_mcraw(
        tmp_path / "labs.mcraw",
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], 1.0),
    )
    report = measure_gyro(path)  # no .gcsv beside it
    assert report.samples == 800  # 2 s at 400 Hz: half a second either side of the frames
    assert report.sample_rate_hz == pytest.approx(400)
    assert report.acceleration is not None
    linked = [group.frame for group in report.angular_velocity if group.frame is not None]
    assert linked
    assert min(linked) == 0
    assert max(linked) == 23
    assert report.frame_note is not None
    assert "own sensor clock" in report.frame_note


def test_clip_information_names_recorded_motion_and_lens_calibration(make_mcraw: Any, tmp_path: Path) -> None:
    from conftest import RECORDED_CONTAINER, RECORDED_FRAME, SYNTHETIC_STEP, recorded_motion

    from mcrawsome.io.clip import Clip
    from mcrawsome.io.describe import describe

    stamps = [i * SYNTHETIC_STEP for i in range(3)]
    path = make_mcraw(
        tmp_path / "labs.mcraw",
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], 0.2, ois_px=1.0),
    )
    with Clip(path) as clip:
        rows = dict(row for section in describe(clip) for row in section[1])
    assert rows["Gyro"].startswith("recorded in the clip: ")
    assert "with the accelerometer" in rows["Gyro"]
    assert "OIS lens shifts" in rows["Gyro"]
    assert rows["Lens calibration"] == (
        "recorded in the clip: focal length 100 px, optical centre 68, 32, rolling shutter 5.3 ms"
    )
