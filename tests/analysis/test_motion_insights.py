"""Loading a RAW clip's gyro, frame times and focus for the motion summary."""

from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.analysis import motion_insights
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import DirectLogInfo
from mcrawsome.io.gyro import GyroLog

FRAMES = 240  # ten seconds at 24 fps


@pytest.fixture
def clip(tmp_path: Path, make_mcraw: Callable[..., Path], make_gcsv: Callable[..., Path]) -> Path:
    source = make_mcraw(tmp_path / "walk.mcraw", [i * 41_666_667 for i in range(FRAMES)])
    make_gcsv(source.with_suffix(".gcsv"), FRAMES, jolt_at=9.6)
    return source


def test_a_raw_clip_has_movement_blur_and_a_trim_from_its_log(clip: Path) -> None:
    motion = motion_insights.load(clip)
    assert motion is not None
    with Clip(clip, metadata_only=True) as recording:
        assert len(motion.centres) == recording.frame_count
    assert motion.duration_s == pytest.approx(10.0, abs=0.01)
    assert motion.summary().movement == "handheld"
    blur = motion.blur()
    assert np.isfinite(blur).all()
    assert blur.max() > 0
    trim = motion.trim()
    assert trim.start_s == 0.0
    assert 9.0 < trim.end_s < 9.6
    assert trim.reasons[0].startswith("End: a handling jolt")
    assert motion.focus is not None
    assert len(motion.focus) == FRAMES
    assert motion.steadiest() is not None


def test_the_quick_load_agrees_on_the_movement(clip: Path) -> None:
    quick, full = motion_insights.load(clip, every_frame=False), motion_insights.load(clip)
    assert quick is not None
    assert full is not None
    assert quick.focus is None
    assert quick.summary() == full.summary()


def test_a_clip_without_a_usable_log_has_no_motion(clip: Path) -> None:
    clip.with_suffix(".gcsv").write_text("not a log\n")
    assert motion_insights.load(clip) is None
    clip.with_suffix(".gcsv").unlink()
    assert motion_insights.load(clip) is None


def test_an_invalid_folder_recipe_falls_back_to_the_default_settings(clip: Path) -> None:
    clip.parent.joinpath("mcrawsome.toml").write_text("[develop]\nrotation = 45\n")
    motion = motion_insights.load(clip)
    assert motion is not None
    assert motion.summary().movement == "handheld"


def video_info(tmp_path: Path, frames: int = 240) -> DirectLogInfo:
    return DirectLogInfo(
        path=tmp_path / "clip_Davinci_Davinci-WG.mp4", width=3840, height=2160, fps=Fraction(24),
        frame_count=frames, duration_s=frames / 24, codec="av1", pix_fmt="yuv420p10le", colour_range="pc",
        encoding="davinci", audio_rate=48000, audio_channels=2, has_telemetry=True,
    )  # fmt: skip


def telemetry(seconds: float, pan_deg_s: float = 0.0) -> GyroLog:
    t = np.arange(0, seconds, 1 / 384)
    gyro = np.zeros((len(t), 3))
    gyro[:, 0] = np.radians(6) * np.sin(2 * np.pi * 4 * t)
    gyro[:, 1] = np.radians(pan_deg_s)
    accel = np.zeros((len(t), 3))
    accel[:, 1] = 9.80665
    return GyroLog(times_s=t, gyro=gyro, accel=accel, start_epoch_s=None, orientation="XYZ", clock="video")


def test_direct_log_motion_uses_its_telemetry_and_assumes_a_half_frame_shutter(tmp_path: Path) -> None:
    motion = motion_insights.video_motion(telemetry(10.5), {"MINF": ""}, video_info(tmp_path), None)
    assert motion.note == motion_insights.LOG_EXPOSURE_NOTE
    assert len(motion.centres) == 240
    assert motion.exposure_s[0] == pytest.approx(1 / 48)
    assert motion.duration_s == pytest.approx(10.0)
    assert motion.summary().movement == "handheld"
    facts = motion.facts()
    assert facts["movement"] == "handheld"
    assert isinstance(facts["shake_blur_px"], float)
    assert facts["shake_blur_px"] > 0
    panning = motion_insights.video_motion(
        telemetry(10.5, pan_deg_s=20), {"MINF": ""}, video_info(tmp_path), None
    )
    assert panning.summary().movement == "pan"


def test_direct_log_without_frames_has_no_motion_rather_than_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cover"):
        motion_insights.video_motion(telemetry(10.5), {}, video_info(tmp_path, frames=0), None)
