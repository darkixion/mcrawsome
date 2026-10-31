"""Profile import is useful before footage has a trusted gyro fit or lens correction pass."""

import json
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from mcrawsome.camera.profile import Axes, CameraProfile, all_profiles, load_profile
from mcrawsome.cli import processing as commands
from mcrawsome.pipeline.calibrate import Recording


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recording:
    clip = tmp_path / "calibration.mp4"
    clip.touch()
    source = Recording(
        clip, "gpmf", "test phone", "main", 1920, 1080, 24.0, None, np.array([]), 0.0, lambda _: []
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(commands, "read_recording", lambda *_: source)
    all_profiles.cache_clear()
    return source


def test_cli_saves_geometry_and_keeps_existing_motion_and_gravity_calibration(
    recording: Recording, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    axes = Axes(axes=(2, 0, 1), signs=(1, -1, 1))
    previous = CameraProfile(
        name="previous", gcsv_axes=axes, gpmf_axes=axes, gcsv_gravity_axes=axes,
        gcsv_gravity_camera_ids=("main",), gcsv_sync_ms=-100, gpmf_sync_ms=-10,
    )  # fmt: skip
    monkeypatch.setattr(commands, "find_profile", lambda *_: previous)
    lens = tmp_path / "lens.json"
    lens.write_text(json.dumps({
        "calib_dimension": {"w": 3840, "h": 2160},
        "distortion_model": "opencv_standard", "frame_readout_time": 18,
        "fisheye_params": {
            "camera_matrix": [[3000, 0, 1920], [0, 2990, 1080], [0, 0, 1]],
            "distortion_coeffs": [0.01, -0.02, 0.003, 0.004, 0.0005],
        },
    }))  # fmt: skip
    args = ["calibrate", str(recording.path), "--from-gyroflow", str(lens), "--name", "measured"]
    dry_run = CliRunner().invoke(commands.cli, args)
    assert dry_run.exit_code == 0, dry_run.output
    target = tmp_path / "config" / "mcrawsome" / "cameras" / "measured.toml"
    assert not target.exists()
    result = CliRunner().invoke(commands.cli, [*args, "--save"])
    assert result.exit_code == 0, result.output
    assert "retained for Gyroflow interchange" in result.output
    assert "does not yet apply lens correction" in result.output
    profile = load_profile(target)
    assert profile.lens_calibration is not None
    assert profile.lens_calibration.coefficients == (0.01, -0.02, 0.003, 0.004, 0.0005)
    assert profile.lens_calibration.width == 3840
    assert profile.match.width == 1920
    assert profile.match.camera_id == "main"
    assert profile.gcsv_axes == previous.gcsv_axes
    assert profile.gpmf_axes == previous.gpmf_axes
    assert profile.gcsv_gravity_axes == previous.gcsv_gravity_axes
    assert profile.gcsv_gravity_camera_ids == previous.gcsv_gravity_camera_ids
    assert profile.gcsv_sync_ms == previous.gcsv_sync_ms
    assert profile.gpmf_sync_ms == previous.gpmf_sync_ms
    assert profile.readout_ms == 18
    all_profiles.cache_clear()


@pytest.mark.parametrize("contents", ["[]", "null", '{"fisheye_params": null}'])
def test_cli_reports_invalid_lens_json_without_a_traceback(
    recording: Recording, tmp_path: Path, contents: str
) -> None:
    lens = tmp_path / "bad.json"
    lens.write_text(contents)
    result = CliRunner().invoke(
        commands.cli, ["calibrate", str(recording.path), "--from-gyroflow", str(lens), "--save"]
    )
    assert result.exit_code == 1
    assert "not a Gyroflow lens profile" in result.output
    assert "--name:" not in result.output
    assert not (tmp_path / "config" / "mcrawsome" / "cameras").exists()
