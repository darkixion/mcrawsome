import json
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from mcrawsome.camera.lens import LensCalibration
from mcrawsome.camera.profile import CameraProfile, Match
from mcrawsome.io.directlog import probe_direct_log
from mcrawsome.motion.flow import measure_strips_video
from mcrawsome.motion.gyroflow import import_gyroflow_profile, lens_profile
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.pipeline import lens_calibrate as capture
from mcrawsome.pipeline.lens_calibrate import (
    LensCaptureOptions,
    LensCaptureResult,
    analyse_camera,
    analyse_lens,
)


def result() -> LensCaptureResult:
    lens = LensCalibration(
        model="opencv_standard",
        width=3840,
        height=2160,
        original_width=3840,
        original_height=2160,
        camera_matrix=((3000, 0, 1920), (0, 3000, 1080), (0, 0, 1)),
        coefficients=(-0.1, 0.02, 0, 0, 0),
        rms_error=0.2,
        image_count=30,
    )
    profile = CameraProfile(
        name="test",
        match=Match(device_model="pixel test", width=3840, height=2160, fps=24),
        focal_px=3000,
        lens_calibration=lens,
    )
    document = lens_profile(profile, (3840, 2160), 24)
    document["mcrawsome"] = {
        "camera_profile": profile.model_dump(mode="json"),
        "unmeasured": ["gyro_axes", "gyro_sync", "rolling_shutter_readout", "gyro_lpf"],
    }
    return LensCaptureResult(profile=profile, document=document, passed=True, issues=[])


def test_sampling_is_bounded_and_covers_the_entire_recording() -> None:
    indices = capture.sample_indices(100000, 24)
    assert len(indices) == capture.MAX_SAMPLES
    assert indices[0] == 0
    assert indices[-1] == 99999
    assert capture.sample_indices(1, 24) == [0]
    with pytest.raises(ValueError, match="frame rate"):
        capture.sample_indices(12, 0)


def test_cancelled_analysis_does_not_open_a_recording() -> None:
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        analyse_lens(Path("missing.mcraw"), LensCaptureOptions(name="test"), stop)


def test_gyro_cancellation_stops_metadata_and_flow_reads(synthetic_video: Path) -> None:
    from mcrawsome.pipeline.calibrate import read_recording

    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        read_recording(Path("missing.mcraw"), 1, stop=stop)
    ffmpeg, probe = find_ffmpeg()
    info = probe_direct_log(probe, synthetic_video)
    with pytest.raises(CancelledError):
        measure_strips_video(ffmpeg, synthetic_video, [0, 1], info.width, info.height, [(0, 1)], stop=stop)


def test_raw_without_checkerboards_gives_a_useful_error(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    path = make_mcraw(tmp_path / "plain.mcraw", [0, 41666667, 83333334])
    with pytest.raises(ValueError, match="distinct sharp boards"):
        analyse_lens(path, LensCaptureOptions(name="plain"), threading.Event())


def test_video_reader_selects_original_frames(synthetic_video: Path) -> None:
    _, probe = find_ffmpeg()
    info = probe_direct_log(probe, synthetic_video)
    images = list(capture._video_images(synthetic_video, info.width, info.height, [0, 2], threading.Event()))
    assert [index for index, _ in images] == [0, 2]
    assert images[0][1].dtype == np.uint8
    assert images[0][1].shape == (info.height, info.width)
    assert not np.array_equal(images[0][1], images[1][1])


def test_video_reader_decodes_all_480_samples_from_a_long_recording(tmp_path: Path) -> None:
    # A 166-second recording reaches the sample limit. Distinct lossless frame contents
    # verify both the parser limit and original frame-number association across gaps.
    frames = np.empty((4000, 24, 32), dtype=np.uint8)
    numbers = np.arange(len(frames))[:, None, None]
    frames[:, :, :16] = numbers % 256
    frames[:, :, 16:] = numbers // 256
    path = tmp_path / "long-calibration.mkv"
    subprocess.run(
        [
            str(find_ffmpeg()[0]),
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pixel_format",
            "gray",
            "-video_size",
            "32x24",
            "-framerate",
            "24",
            "-i",
            "-",
            "-c:v",
            "ffv1",
            str(path),
        ],
        input=frames.tobytes(),
        check=True,
    )
    indices = capture.sample_indices(len(frames), 24)
    assert len(indices) == 480
    decoded = list(capture._video_images(path, 32, 24, indices, threading.Event()))
    assert [index for index, _ in decoded] == indices
    for index, image in decoded:
        np.testing.assert_array_equal(image, frames[index])


def test_gyro_measurement_preserves_optical_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_calibrate import _log, _recording

    original = result()
    monkeypatch.setattr(capture, "analyse_lens", lambda *args: original)
    monkeypatch.setattr(capture, "read_recording", lambda *args, **kwargs: _recording(_log()))
    measured = analyse_camera(
        Path("board.mcraw"),
        LensCaptureOptions(name="test"),
        threading.Event(),
        gyro_path=Path("motion.mcraw"),
    )
    assert measured.profile.lens_calibration == original.profile.lens_calibration
    assert measured.profile.focal_px == original.profile.focal_px
    assert measured.profile.gpmf_axes is not None
    assert measured.profile.readout_ms == pytest.approx(18, abs=2)
    assert measured.document["frame_readout_time"] == measured.profile.readout_ms
    assert measured.document["mcrawsome"]["unmeasured"] == ["gyro_lpf"]
    # The same exported document is valid Gyroflow geometry, with extra diagnostics ignored.
    exported: dict[str, Any] = json.loads(json.dumps(measured.document, allow_nan=False))
    imported = import_gyroflow_profile(exported, 3840, 2160)
    assert imported.lens_calibration is not None
    assert original.profile.lens_calibration is not None
    assert imported.lens_calibration.coefficients[:5] == original.profile.lens_calibration.coefficients


def test_different_gyro_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from test_calibrate import _log, _recording

    monkeypatch.setattr(capture, "analyse_lens", lambda *args: result())
    monkeypatch.setattr(
        capture, "read_recording", lambda *args, **kwargs: replace(_recording(_log()), width=1920)
    )
    with pytest.raises(ValueError, match="same camera"):
        analyse_camera(
            Path("board.mcraw"),
            LensCaptureOptions(name="test"),
            threading.Event(),
            gyro_path=Path("motion.mcraw"),
        )


def test_real_checkerboard_video_produces_a_complete_interchange_document(tmp_path: Path) -> None:
    """Render known planar poses, encode them, then exercise decoding, detection, solving and JSON."""
    rng = np.random.default_rng(28)
    pattern = np.full((540, 720), 255, np.uint8)
    for row in range(7):
        for column in range(10):
            if (row + column) % 2 == 0:
                pattern[(row + 1) * 60 : (row + 2) * 60, (column + 1) * 60 : (column + 2) * 60] = 0
    matrix = np.array([[450.0, 0, 320], [0, 448.0, 240], [0, 0, 1]])
    objects = np.array([[-2.0, -2, 0], [10, -2, 0], [10, 7, 0], [-2, 7, 0]])
    source = np.array([[0.0, 0], [720, 0], [720, 540], [0, 540]], np.float32)
    frames = []
    for _ in range(70):
        rotation = rng.uniform([-0.45, -0.5, -0.2], [0.45, 0.5, 0.2])
        translation = np.array([-4.0, -2.5, rng.uniform(9, 18)]) + rng.uniform([-2, -2, 0], [2, 2, 0])
        points, _ = cv2.projectPoints(objects, rotation, translation, matrix, np.zeros(5))
        transform = cv2.getPerspectiveTransform(source, points.reshape(-1, 2).astype(np.float32))
        frames.append(cv2.warpPerspective(pattern, transform, (640, 480), borderValue=180).tobytes())
    path = tmp_path / "checkerboards.mkv"
    subprocess.run(
        [
            str(find_ffmpeg()[0]),
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pixel_format",
            "gray",
            "-video_size",
            "640x480",
            "-framerate",
            "3",
            "-i",
            "-",
            "-c:v",
            "ffv1",
            str(path),
        ],
        input=b"".join(frames),
        check=True,
    )
    measured = analyse_lens(
        path, LensCaptureOptions(name="rendered_camera", camera_model="Synthetic camera"), threading.Event()
    )
    document = json.loads(json.dumps(measured.document, allow_nan=False))
    assert measured.passed, measured.issues
    assert document["fisheye_params"]["camera_matrix"][0][0] == pytest.approx(450, rel=0.04)
    assert document["fisheye_params"]["RMS_error"] < 0.5
    assert document["mcrawsome"]["quality"]["held_out_rms_px"] < 0.5
    assert document["frame_readout_time"] is None
    assert document["num_images"] >= 15
    assert document["calib_dimension"] == {"w": 640, "h": 480}
    # Every top-level field in the user's Pixel 8 reference is represented, including unknowns.
    expected = (
        "name note calibrated_by camera_brand camera_model lens_model camera_setting calib_dimension "
        "orig_dimension output_dimension frame_readout_time gyro_lpf input_horizontal_stretch "
        "input_vertical_stretch num_images fps crop official asymmetrical fisheye_params identifier "
        "calibrator_version date compatible_settings sync_settings distortion_model digital_lens "
        "digital_lens_params interpolations focal_length crop_factor global_shutter"
    )
    assert set(expected.split()) <= document.keys()
