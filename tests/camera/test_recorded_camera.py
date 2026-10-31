"""The camera as a MotionCam 5.1 recording describes it: Android's lens calibration and sensor axes."""

from typing import Any

import numpy as np
import pytest

from mcrawsome.camera.profile import Axes
from mcrawsome.camera.recorded import (
    RecordedCamera,
    device_to_camera,
    gravity_axes,
    gyro_axes,
    recorded_camera,
)
from mcrawsome.io.metadata import ContainerMetadata, FrameTiming

CONTAINER: dict[str, Any] = {
    "blackLevel": [64, 64, 64, 64],
    "whiteLevel": 1023,
    "sensorArrangment": "rggb",
    "colorMatrix1": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    "colorMatrix2": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    "forwardMatrix1": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    "forwardMatrix2": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    "sensorOrientation": 90,
    "lensFacing": 1,
}
# 261028_210309's first frame: a 3840 x 2160 centre crop of the 4080 x 3072 array, unbinned.
FRAME: dict[str, Any] = {
    "width": 3840,
    "height": 2160,
    "orientation": 2,
    "exposureTime": 20825846.0,
    "recvdTimestampMs": 0,
    "originalWidth": 4080,
    "originalHeight": 3072,
    "isBinned": False,
    "lensIntrinsicCalibration": [
        2916.37255859375,
        2916.37255859375,
        2045.2811279296875,
        1522.458740234375,
        0.0,
    ],
    "lensDistortion": [0.0501, -0.1029, 0.0759, 0.001, 0.002],
    "rollingShutterSkewNs": "19469568",
}


def _camera(
    container: dict[str, Any] | None = None, frame: dict[str, Any] | None = None
) -> RecordedCamera | None:
    meta = ContainerMetadata.model_validate(CONTAINER | (container or {}))
    return recorded_camera(meta, FrameTiming.model_validate(FRAME | (frame or {})))


def test_the_recorded_calibration_is_moved_to_the_recorded_crop() -> None:
    camera = _camera()
    assert camera is not None
    assert camera.focal_px == pytest.approx(2916.37, abs=0.01)
    assert camera.centre_px == pytest.approx((2045.28 - 120, 1522.46 - 456), abs=0.01)
    assert camera.distortion == (0.0501, -0.1029, 0.001, 0.002, 0.0759)  # OpenCV's k1, k2, p1, p2, k3
    assert camera.readout_s == pytest.approx(0.019469568 * 2160 / 3072)  # the recorded rows only
    assert camera.first_row_s == pytest.approx(0.019469568 * 456 / 3072)
    assert camera.shift_scale == 1
    lens = camera.lens_calibration()
    assert lens is not None
    assert lens.matrix_at(3840, 2160)[0] == pytest.approx([2916.37, 0, 1925.28], abs=0.01)


def test_a_binned_frame_halves_the_calibration() -> None:
    camera = _camera(frame={"width": 2040, "height": 1536, "isBinned": True})
    assert camera is not None
    assert camera.focal_px == pytest.approx(2916.37 / 2, abs=0.01)
    assert camera.centre_px == pytest.approx((2045.28 / 2, 1522.46 / 2), abs=0.01)
    assert camera.readout_s == pytest.approx(0.019469568)
    assert camera.first_row_s == 0
    assert camera.shift_scale == 0.5


def test_the_container_calibration_stands_in_for_a_frame_without_one() -> None:
    camera = _camera({"lensIntrinsicCalibration": [2867.3, 2867.3, 2048.7, 1519.6, 0.0]}, {
        "lensIntrinsicCalibration": None,
    })  # fmt: skip
    assert camera is not None
    assert camera.focal_px == pytest.approx(2867.3)


@pytest.mark.parametrize(
    "frame",
    [{"lensIntrinsicCalibration": None}, {"lensIntrinsicCalibration": [0.0, 0.0, 10.0, 10.0, 0.0]}],
)
def test_no_usable_calibration_means_no_recorded_camera(frame: dict[str, Any]) -> None:
    assert _camera(frame=frame) is None


def test_an_implausible_skew_leaves_the_readout_unknown() -> None:
    camera = _camera(frame={"rollingShutterSkewNs": "0"})
    assert camera is not None
    assert camera.readout_s is None
    assert camera.first_row_s == 0


def test_a_back_camera_at_90_degrees_has_the_axes_measured_from_its_gcsv_logs() -> None:
    # The built-in Pixel 10 Pro XL profile's gcsv_axes and gcsv_gravity_axes, measured from the picture.
    camera = _camera()
    assert camera is not None
    assert camera.gyro_axes == Axes(axes=(0, 1, 2), signs=(1, -1, 1))
    assert camera.gravity_axes == Axes(axes=(1, 0, 2), signs=(-1, -1, -1))


@pytest.mark.parametrize(("angle", "facing"), [(0, 1), (90, 1), (180, 1), (270, 1), (90, 0), (270, 0)])
def test_every_sensor_orientation_gives_a_rotation(angle: int, facing: int) -> None:
    meta = ContainerMetadata.model_validate(CONTAINER | {"sensorOrientation": angle, "lensFacing": facing})
    matrix = device_to_camera(meta)
    assert matrix is not None
    assert np.allclose(matrix @ matrix.T, np.eye(3))
    assert np.linalg.det(matrix) == pytest.approx(1)
    assert matrix[2, 2] == (-1 if facing == 1 else 1)  # the lens looks out of the back, or the screen
    for axes in (gyro_axes(matrix), gravity_axes(matrix)):
        assert sorted(axes.axes) == [0, 1, 2]


@pytest.mark.parametrize("fields", [{"lensFacing": None}, {"lensFacing": 2}, {"sensorOrientation": 45}])
def test_unknown_lens_facing_or_angle_gives_no_axes(fields: dict[str, Any]) -> None:
    camera = _camera(fields)
    assert camera is not None
    assert camera.gyro_axes is None
    assert camera.gravity_axes is None
