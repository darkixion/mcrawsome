"""Synthetic target projections only; no personal calibration footage."""

import threading
import xml.etree.ElementTree as ET
from concurrent.futures import CancelledError
from pathlib import Path

import cv2
import numpy as np
import pytest

from mcrawsome.camera.calibrate_lens import Model, analyse_lens, solve_lens, target_svg
from mcrawsome.pipeline.calibrate import CalibrationError


def views(model: str) -> tuple[list[np.ndarray], np.ndarray, np.ndarray]:
    points = np.zeros((54, 3), np.float64)
    points[:, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2)
    matrix = np.array([[520.0, 0.0, 320.0], [0.0, 515.0, 240.0], [0.0, 0.0, 1.0]])
    distortion = (
        np.array([-0.08, 0.015, 0.0005, -0.0003, 0.001])
        if model == "opencv_standard"
        else np.array([-0.03, 0.003, 0.0002, -0.0001])
    )
    rng = np.random.default_rng(12)
    found = []
    for _ in range(24):
        rotation = rng.uniform([-0.5, -0.5, -0.2], [0.5, 0.5, 0.2])
        translation = np.array(
            [-4.0 + rng.uniform(-2.8, 2.8), -2.5 + rng.uniform(-2.0, 2.0), rng.uniform(13.0, 19.0)]
        )
        if model == "opencv_standard":
            projected = cv2.projectPoints(points, rotation, translation, matrix, distortion)[0]
        else:
            projected = cv2.fisheye.projectPoints(
                points.reshape(1, -1, 3), rotation, translation, matrix, distortion
            )[0]
        found.append(projected.reshape(-1, 1, 2).astype(np.float32))
    return found, matrix, distortion


def test_target_has_the_expected_printable_grid_and_capture_instructions() -> None:
    document = ET.fromstring(target_svg())
    squares = [
        node for node in document.iter() if node.tag.endswith("rect") and node.attrib.get("fill") == "black"
    ]
    assert len(squares) == 35
    assert "Move the phone" in target_svg()
    assert "Keep the target flat" in target_svg()


@pytest.mark.parametrize("model", ["opencv_standard", "opencv_fisheye"])
def test_recovers_measured_lens_geometry_from_varied_synthetic_views(model: Model) -> None:
    corners, matrix, distortion = views(model)
    measured = solve_lens(corners, (640, 480), (1280, 960), model)
    np.testing.assert_allclose(measured.camera_matrix, matrix, atol=0.5)
    np.testing.assert_allclose(measured.coefficients, distortion, atol=0.005)
    assert measured.original_width == 1280
    assert measured.image_count == 24
    assert measured.rms_error is not None
    assert measured.rms_error < 0.01


def test_rejects_a_stationary_target_instead_of_creating_a_misleading_profile() -> None:
    corners, _, _ = views("opencv_standard")
    with pytest.raises(CalibrationError, match="different views"):
        solve_lens([corners[0]] * 20, (640, 480), (1280, 960), "opencv_standard")


def test_rejects_an_insufficient_recording() -> None:
    corners, _, _ = views("opencv_standard")
    with pytest.raises(CalibrationError, match="12"):
        solve_lens(corners[:5], (640, 480), (1280, 960), "opencv_standard")


def test_cancelled_analysis_does_not_read_a_recording(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        analyse_lens(tmp_path / "unopened.mcraw", "opencv_standard", stop)


def test_target_detection_records_phone_module_and_preserves_existing_gyro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from mcrawsome.camera import calibrate_lens as module
    from mcrawsome.camera.profile import Axes, CameraProfile

    matrix = np.array([[520.0, 0.0, 320.0], [0.0, 515.0, 240.0], [0.0, 0.0, 1.0]])
    outer = np.array([[-1.0, -1.0, 0.0], [9.0, -1.0, 0.0], [9.0, 6.0, 0.0], [-1.0, 6.0, 0.0]])
    board = np.ones((700, 1000), np.uint8) * 255
    for y in range(7):
        for x in range(10):
            if (x + y) % 2 == 0:
                board[y * 100 : (y + 1) * 100, x * 100 : (x + 1) * 100] = 0
    images = []
    rng = np.random.default_rng(12)
    for _ in range(30):
        rotation = rng.uniform([-0.5, -0.5, -0.2], [0.5, 0.5, 0.2])
        translation = np.array(
            [-4.0 + rng.uniform(-2.8, 2.8), -2.5 + rng.uniform(-2.0, 2.0), rng.uniform(13.0, 19.0)]
        )
        projection = cv2.projectPoints(outer, rotation, translation, matrix, np.zeros(5))[0].reshape(4, 2)
        transform = cv2.getPerspectiveTransform(
            np.array([[0, 0], [1000, 0], [1000, 700], [0, 700]], np.float32), projection.astype(np.float32)
        )
        images.append(cv2.warpPerspective(board, transform, (640, 480), borderValue=255))

    class Reader:
        metadata = SimpleNamespace(device_model="Test phone", camera_id="0/2", sensor_arrangement="rggb")
        fps = 24.0
        frame_count = len(images)

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "Reader":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def frame_metadata(self, index: int) -> SimpleNamespace:
            return SimpleNamespace(width=1280, height=960)

        def raw_frame(self, index: int) -> SimpleNamespace:
            raw = np.repeat(np.repeat(images[index].astype(np.uint16) * 200, 2, axis=0), 2, axis=1)
            return SimpleNamespace(data=raw, metadata=self.frame_metadata(index))

        def check_unchanged(self) -> None:
            pass

    seed = CameraProfile(
        name="known", readout_ms=18.0, gcsv_sync_ms=-105.0, gcsv_axes=Axes(axes=(0, 1, 2), signs=(1, -1, 1))
    )
    monkeypatch.setattr(module, "Clip", Reader)
    monkeypatch.setattr(module, "find_profile", lambda *args: seed)
    result = analyse_lens(tmp_path / "synthetic.mcraw", "opencv_standard", threading.Event())
    assert result.profile.match.device_model == "Test phone"
    assert result.profile.match.camera_id == "0/2"
    assert result.profile.match.width == 1280
    assert result.profile.gcsv_axes == seed.gcsv_axes
    assert result.profile.gcsv_sync_ms == -105.0
    assert result.profile.lens_calibration is not None
    assert result.profile.lens_calibration.image_count >= 12
    assert result.profile.lens_calibration.rms_error is not None
    assert result.profile.lens_calibration.rms_error < 1.0
    np.testing.assert_allclose(result.profile.lens_calibration.camera_matrix, matrix, atol=6)
