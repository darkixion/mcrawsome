import json
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.camera.profile import CameraProfile, Match, ProfileError
from mcrawsome.io.gcsv import read_gcsv
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.gyroflow import import_gyroflow_profile, lens_profile, synced_gcsv


def _profile() -> CameraProfile:
    return CameraProfile(
        name="pixel_10_pro_xl_4k",
        match=Match(device_model="pixel 10 pro xl", width=3840, height=2160),
        focal_px=2990.0,
        readout_ms=18.0,
    )


def test_lens_profile_has_gyroflows_fields() -> None:
    data = lens_profile(
        _profile(), (3840, 2160), 23.976, camera_brand="Google", camera_model="Pixel 10 Pro XL"
    )
    json.dumps(data)  # serialisable
    assert data["calibrated_by"] == "MCRAWsome"
    assert data["camera_brand"] == "Google"
    assert data["camera_model"] == "Pixel 10 Pro XL"
    assert data["lens_model"]
    assert data["calib_dimension"] == {"w": 3840, "h": 2160}
    assert data["orig_dimension"] == {"w": 3840, "h": 2160}
    assert data["frame_readout_time"] == pytest.approx(18.0)
    assert data["fps"] == pytest.approx(23.976)
    assert data["distortion_model"] == "opencv_standard"
    matrix = data["fisheye_params"]["camera_matrix"]
    assert matrix == [[2990.0, 0.0, 1920.0], [0.0, 2990.0, 1080.0], [0.0, 0.0, 1.0]]
    coeffs = data["fisheye_params"]["distortion_coeffs"]
    assert len(coeffs) == 12  # Gyroflow's OpenCV standard model reads twelve coefficients
    assert not any(coeffs)


def test_lens_profile_without_a_readout_says_so() -> None:
    data = lens_profile(_profile().model_copy(update={"readout_ms": None}), (3840, 2160), 24.0)
    assert data["frame_readout_time"] is None


def _gyroflow_json(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "Google_Pixel 7 Pro_Main_4k",
        "camera_brand": "Google",
        "camera_model": "Pixel 7 Pro",
        "calib_dimension": {"w": 4000, "h": 3000},
        "orig_dimension": {"w": 4000, "h": 3000},
        "frame_readout_time": 16.5,
        "fps": 30.0,
        "fisheye_params": {
            "RMS_error": 0.4,
            "camera_matrix": [[3100.0, 0.0, 2000.0], [0.0, 3090.0, 1500.0], [0.0, 0.0, 1.0]],
            "distortion_coeffs": [0.01, -0.02, 0.0, 0.0],
        },
        "distortion_model": "opencv_fisheye",
    }
    data.update(changes)
    return data


def test_gyroflow_profile_imports_scaled_to_the_clip() -> None:
    profile = import_gyroflow_profile(_gyroflow_json(), 3840, 2160)
    assert profile.focal_px == pytest.approx(3095.0 * 3840 / 4000)
    assert profile.readout_ms == pytest.approx(16.5)
    assert profile.gcsv_axes is None  # axes and sync still come from each clip's fit
    assert profile.gcsv_sync_ms is None
    assert "Pixel 7 Pro" in profile.description


def test_gyroflow_profile_with_a_negative_or_missing_readout() -> None:
    assert import_gyroflow_profile(_gyroflow_json(frame_readout_time=-16.5), 3840, 2160).readout_ms == 16.5
    assert import_gyroflow_profile(_gyroflow_json(frame_readout_time=None), 3840, 2160).readout_ms is None


def test_a_file_that_is_not_a_lens_profile_is_a_clear_error() -> None:
    with pytest.raises(ProfileError, match="camera matrix"):
        import_gyroflow_profile({"name": "x"}, 3840, 2160)


def test_synced_gcsv_round_trips_with_shifted_times(tmp_path: Path) -> None:
    rng = np.random.default_rng(2)
    times = np.arange(0, 3, 1 / 400)
    log = GyroLog(
        times_s=times,
        gyro=rng.normal(0, 0.5, (len(times), 3)),
        accel=rng.normal(0, 9, (len(times), 3)),
        start_epoch_s=1791132082.639,
        orientation="XYZ",
        header={"id": "Google.Pixel.10.Pro.XL", "vendor": "MotionCam"},
    )
    path = tmp_path / "synced.gcsv"
    path.write_text(synced_gcsv(log, 0.4321), encoding="utf-8")
    back = read_gcsv(path)
    np.testing.assert_allclose(back.times_s, times - 0.4321, atol=1e-6)
    np.testing.assert_allclose(back.gyro, log.gyro, atol=1e-8)
    assert back.accel is not None
    assert log.accel is not None
    np.testing.assert_allclose(back.accel, log.accel, atol=1e-8)
    assert back.start_epoch_s == pytest.approx(1791132082.639 + 0.4321, abs=1e-3)
    assert back.orientation == "XYZ"
    assert back.header["id"] == "Google.Pixel.10.Pro.XL"
