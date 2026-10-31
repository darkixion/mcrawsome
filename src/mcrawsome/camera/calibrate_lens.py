"""Measure lens geometry from the supplied stationary target, without changing recordings."""

from __future__ import annotations

from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from typing import Literal

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

from mcrawsome.camera.lens import LensCalibration
from mcrawsome.camera.profile import CameraProfile, Match, find_profile, profile_name
from mcrawsome.io.clip import Clip
from mcrawsome.pipeline.calibrate import CalibrationError

Model = Literal["opencv_standard", "opencv_fisheye"]
Points = npt.NDArray[np.float32]
PATTERN = (9, 6)
SAMPLES = 60


class LensAnalysis(BaseModel):
    profile: CameraProfile
    sampled_images: int
    detected_images: int
    notes: list[str]


def target_svg() -> str:
    squares = "".join(
        f'<rect x="{242 + x * 100}" y="{150 + y * 100}" width="100" height="100" fill="black"/>'
        for y in range(7)
        for x in range(10)
        if (x + y) % 2 == 0
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" viewBox="0 0 1485 1050">'
        '<title>MCRAWsome lens calibration target</title><rect width="1485" height="1050" fill="white"/>'
        '<g fill="black" font-family="sans-serif">'
        '<text x="60" y="50" font-size="26">MCRAWsome lens calibration: 10 x 7 squares</text></g>'
        + squares
        + '<g fill="black" font-family="sans-serif" font-size="22">'
        '<text x="60" y="960">Move the phone: left/right, up/down, nearer/further; tilt and roll.</text>'
        '<text x="60" y="1000">Keep the target flat and still. Keep all squares visible. Lock focus.</text>'
        "</g></svg>"
    )


def _fisheye_flag(name: str) -> int:
    value = getattr(cv2.fisheye, name, None)
    return int(getattr(cv2, name) if value is None else value)


def solve_lens(
    corners: list[Points], size: tuple[int, int], original: tuple[int, int], model: Model
) -> LensCalibration:
    if len(corners) < 12:
        raise CalibrationError("Need at least 12 clear target views; keep the whole board visible")
    width, height = size
    unique: list[Points] = []
    scale = np.array([width, height])
    for points in corners:
        if points.shape != (54, 1, 2) or not np.isfinite(points).all():
            raise CalibrationError("Target corners are incomplete or invalid")
        if all(float(np.sqrt(np.mean(((points - old) / scale) ** 2))) > 0.025 for old in unique):
            unique.append(points)
    if len(unique) < 8:
        raise CalibrationError(
            "Need more different views; move and tilt the phone around the stationary target"
        )
    joined = np.concatenate(corners).reshape(-1, 2)
    coverage = np.ptp(joined, axis=0) / scale
    if np.any(coverage < 0.55):
        raise CalibrationError(
            "Move the target views closer to all four image edges, keeping every square visible"
        )
    tilted = 0
    for points in corners:
        grid = points.reshape(6, 9, 2)
        top, bottom = np.linalg.norm(grid[0, -1] - grid[0, 0]), np.linalg.norm(grid[-1, -1] - grid[-1, 0])
        left, right = np.linalg.norm(grid[-1, 0] - grid[0, 0]), np.linalg.norm(grid[-1, -1] - grid[0, -1])
        tilted += int(
            min(top, bottom, left, right) > 0
            and max(abs(np.log(top / bottom)), abs(np.log(left / right))) > 0.03
        )
    if tilted < 3:
        raise CalibrationError(
            "Tilt the phone sideways and up/down; a front-on view alone cannot calibrate the lens"
        )
    objects = np.zeros((54, 3), np.float32)
    objects[:, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-8)
    try:
        if model == "opencv_fisheye":
            rms, matrix, coefficients, _, _ = cv2.fisheye.calibrate(
                [objects.astype(np.float64).reshape(1, -1, 3) for _ in corners],
                [points.astype(np.float64).reshape(1, -1, 2) for points in corners],
                size,
                np.zeros((3, 3), np.float64),
                np.zeros((4, 1), np.float64),
                flags=_fisheye_flag("CALIB_RECOMPUTE_EXTRINSIC")
                | _fisheye_flag("CALIB_CHECK_COND")
                | _fisheye_flag("CALIB_FIX_SKEW"),
                criteria=criteria,
            )
        else:
            rms, matrix, coefficients, _, _ = cv2.calibrateCamera(
                [objects for _ in corners],
                corners,
                size,
                None,
                None,
                criteria=criteria,
            )
    except cv2.error as exc:
        raise CalibrationError(
            "The target views could not constrain this lens model; try more angles and distances"
        ) from exc
    if not np.isfinite(rms) or rms > 1.0 or not np.isfinite(matrix).all():
        raise CalibrationError("Target fit is too poor; lock focus, avoid blur and keep the target flat")
    if not (0.1 * width < matrix[0, 0] < 10 * width and 0.1 * height < matrix[1, 1] < 10 * height):
        raise CalibrationError("The fitted focal length is implausible; record more varied target angles")
    return LensCalibration(
        model=model,
        width=width,
        height=height,
        original_width=original[0],
        original_height=original[1],
        camera_matrix=tuple(tuple(float(value) for value in row) for row in matrix),
        coefficients=tuple(float(value) for value in coefficients.ravel()),
        asymmetrical=bool(abs(matrix[0, 2] - width / 2) > 0.5 or abs(matrix[1, 2] - height / 2) > 0.5),
        rms_error=float(rms),
        image_count=len(corners),
    )


def analyse_lens(source: Path, model: Model, stop: Event) -> LensAnalysis:
    def check() -> None:
        if stop.is_set():
            raise CancelledError

    check()
    corners: list[Points] = []
    size: tuple[int, int] | None = None
    with Clip(source, quad_bayer_remosaic=False) as clip:
        metadata, fps = clip.metadata, clip.fps
        if not metadata.device_model or not metadata.camera_id:
            raise CalibrationError("This recording must identify its phone model and camera module")
        first = clip.frame_metadata(0)
        original = (first.width, first.height)
        indices = np.unique(np.linspace(0, clip.frame_count - 1, min(SAMPLES, clip.frame_count), dtype=int))
        for index in indices:
            check()
            raw = clip.raw_frame(int(index))
            if (raw.metadata.width, raw.metadata.height) != original:
                raise CalibrationError("Recording resolution changes during calibration")
            # Equal-area green sites retain the RAW field of view without developing colours.
            cfa = clip.metadata.sensor_arrangement
            gy, gx = (0, 0) if cfa[0] == "g" else (0, 1)
            green = (raw.data[gy::2, gx::2].astype(np.float32) + raw.data[gy + 1 :: 2, 1 - gx :: 2]) * 0.5
            while green.shape[1] > 1280 and green.shape[0] % 2 == 0 and green.shape[1] % 2 == 0:
                green = np.asarray(
                    cv2.resize(
                        green,
                        (green.shape[1] // 2, green.shape[0] // 2),
                        interpolation=cv2.INTER_AREA,
                    ),
                    dtype=np.float32,
                )
            low, high = np.percentile(green, [1, 99])
            grey = np.asarray(np.clip((green - low) * (255 / max(1.0, high - low)), 0, 255), dtype=np.uint8)
            size = (grey.shape[1], grey.shape[0])
            found, points = cv2.findChessboardCornersSB(grey, PATTERN, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
            if found:
                corners.append(np.asarray(points, dtype=np.float32).reshape(-1, 1, 2))
        clip.check_unchanged()
        check()
        assert size is not None
        lens = solve_lens(corners, size, original, model)
        check()
        clip.check_unchanged()
    previous = find_profile(metadata.device_model, metadata.camera_id, *original, fps)
    match = Match(
        device_model=metadata.device_model,
        camera_id=metadata.camera_id,
        width=original[0],
        height=original[1],
        fps=fps,
    )
    # Reuse naming without measuring gyro motion or assuming this target calibrates timing.
    name = profile_name(metadata.device_model, metadata.camera_id, *original) + "_lens"
    base = previous or CameraProfile(name=name)
    matrix = lens.matrix_at(*original)
    profile = base.model_copy(
        update={
            "name": name,
            "match": match,
            "lens_calibration": lens,
            "focal_px": (matrix[0][0] + matrix[1][1]) / 2,
            "description": (
                f"{metadata.device_model}, camera {metadata.camera_id}, measured stationary checkerboard"
            ),
        }
    )
    return LensAnalysis(
        profile=profile,
        sampled_images=len(indices),
        detected_images=len(corners),
        notes=[
            "Measured lens geometry is saved for exchange. Image distortion correction is not yet applied.",
            "Lens calibration does not measure gyro timing. Matching existing gyro calibration is retained.",
        ],
    )
