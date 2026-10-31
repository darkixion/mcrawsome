"""The camera as the recording describes it: Android's own lens calibration, which MotionCam 5.1 and later
writes into the .mcraw beside its motion, so stabilising such a clip needs no camera profile.

The calibration refers to the sensor's pre-correction active array; a recorded frame is a centred crop of it,
read whole or binned two by two, as MotionCam records it. The gyro and gravity axes follow from the sensor's
orientation and the way the lens faces, as Android defines them: for the back camera at 90 degrees, the
picture's x runs along the device's -Y, its y along -X, and the lens looks along -Z.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.lens import LensCalibration
from mcrawsome.camera.profile import Axes
from mcrawsome.io.metadata import ContainerMetadata, FrameTiming

LENS_FRONT, LENS_BACK = 0, 1
MAX_SKEW_NS = 100_000_000  # a readout longer than 100 ms is not a rolling shutter


@dataclass(frozen=True)
class RecordedCamera:
    focal_px: float  # at the recorded frame size
    centre_px: tuple[float, float]  # the optical centre in the recorded frame
    distortion: tuple[float, float, float, float, float] | None  # OpenCV standard k1, k2, p1, p2, k3
    readout_s: float | None  # the rolling shutter, first recorded row to last
    first_row_s: float  # how long after the frame's timestamp its first recorded row starts
    gyro_axes: Axes | None  # the device's gyro axes that move the picture (as CameraProfile.gcsv_axes)
    gravity_axes: Axes | None  # device acceleration in picture axes (as CameraProfile.gcsv_gravity_axes)
    shift_scale: float  # recorded pixels for each pixel of lens shift (one over the binning)
    width: int
    height: int

    def lens_calibration(self) -> LensCalibration | None:
        if self.distortion is None:
            return None
        cx, cy = self.centre_px
        return LensCalibration(
            model="opencv_standard",
            width=self.width,
            height=self.height,
            original_width=self.width,
            original_height=self.height,
            camera_matrix=((self.focal_px, 0.0, cx), (0.0, self.focal_px, cy), (0.0, 0.0, 1.0)),
            coefficients=self.distortion,
            asymmetrical=True,
        )


def device_to_camera(meta: ContainerMetadata) -> npt.NDArray[np.float64] | None:
    """The rotation taking Android's device (sensor) axes to the camera's: x along the picture's rows, y down
    its columns, z along the line of sight. None without the sensor orientation or the way the lens faces."""
    angle, facing = meta.sensor_orientation, meta.lens_facing
    if angle is None or angle % 90 or facing not in (LENS_FRONT, LENS_BACK):
        return None
    s, c = _sin_cos(360 - angle if facing == LENS_FRONT else angle)
    matrix = np.array([[c, -s, 0], [-s, -c, 0], [0, 0, -1]], dtype=np.float64)
    if facing == LENS_FRONT:  # looking out of the screen: half a turn about the picture's y axis
        matrix[[0, 2]] *= -1
    return matrix


def _sin_cos(angle: int) -> tuple[int, int]:
    return {0: (0, 1), 90: (1, 0), 180: (0, -1), 270: (-1, 0)}[angle % 360]


def _axes(rows: npt.NDArray[np.float64]) -> Axes:
    """The signed device axis each row reads (rows from a whole-quarter-turn rotation)."""
    axes = tuple(int(np.argmax(np.abs(row))) for row in rows)
    signs = tuple(int(np.sign(row[axis])) for row, axis in zip(rows, axes, strict=True))
    return Axes(axes=axes, signs=signs)


def gyro_axes(matrix: npt.NDArray[np.float64]) -> Axes:
    """The gyro mapping the sync fit uses: turning about the camera's y moves the picture along x against
    the turn, turning about its x moves it along y with the turn, and roll turns against the line of sight."""
    return _axes(np.stack([-matrix[1], matrix[0], -matrix[2]]))


def gravity_axes(matrix: npt.NDArray[np.float64]) -> Axes:
    return _axes(matrix)


def recorded_camera(meta: ContainerMetadata, frame: FrameTiming) -> RecordedCamera | None:
    """The recording's own camera at its recorded frame size, or None when it carries no lens calibration."""
    intrinsics = frame.lens_intrinsic_calibration or meta.lens_intrinsic_calibration
    if intrinsics is None or len(intrinsics) < 4 or min(intrinsics[0], intrinsics[1]) <= 0:
        return None
    binning = 2 if frame.is_binned else 1
    full_width = frame.original_width or frame.width * binning
    full_height = frame.original_height or frame.height * binning
    left = (full_width - frame.width * binning) / 2
    top = (full_height - frame.height * binning) / 2
    fx, fy, cx, cy = intrinsics[:4]
    distortion = frame.lens_distortion or meta.lens_distortion
    skew = frame.rolling_shutter_skew_ns
    readout = first_row = None
    if skew is not None and 0 < skew < MAX_SKEW_NS and full_height > 0:
        readout = skew * frame.height * binning / full_height / 1e9
        first_row = skew * top / full_height / 1e9
    matrix = device_to_camera(meta)
    return RecordedCamera(
        focal_px=(fx + fy) / 2 / binning,
        centre_px=((cx - left) / binning, (cy - top) / binning),
        distortion=(
            (distortion[0], distortion[1], distortion[3], distortion[4], distortion[2])
            if distortion is not None and len(distortion) == 5
            else None
        ),
        readout_s=readout,
        first_row_s=first_row or 0.0,
        gyro_axes=gyro_axes(matrix) if matrix is not None else None,
        gravity_axes=gravity_axes(matrix) if matrix is not None else None,
        shift_scale=1 / binning,
        width=frame.width,
        height=frame.height,
    )
