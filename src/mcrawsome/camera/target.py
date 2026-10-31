"""Screen checkerboard calibration with independent validation and capture diagnostics.

Only observed image points supply lens geometry. Screen millimetres are unnecessary: object
coordinates use square widths. All residuals and intrinsics use the original recording pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.camera.lens import LensCalibration

CORNERS = (9, 6)  # 10 by 7 square cells; shared with the on-screen target
MIN_VIEWS = 20
MAX_VIEWS = 90
MAX_RMS_PX = 1.5
Model = Literal["opencv_standard", "opencv_fisheye"]
Points = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Observation:
    frame: int
    time_s: float
    points: Points
    sharpness: float = 0.0


def object_points() -> Points:
    result = np.zeros((CORNERS[0] * CORNERS[1], 3), np.float64)
    result[:, :2] = np.mgrid[: CORNERS[0], : CORNERS[1]].T.reshape(-1, 2)
    return result


def detect(
    image: npt.NDArray[np.uint8], size: tuple[int, int], frame: int, time_s: float
) -> Observation | None:
    """Complete, sharp boards only; instructions live outside the board's quiet white border."""
    if image.ndim != 2 or min(image.shape) < 80 or np.std(image) < 12:
        return None
    sharpness = float(cv2.Laplacian(image, cv2.CV_64F).var())
    if sharpness < 25:
        return None
    found, corners = cv2.findChessboardCornersSB(image, CORNERS, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not found:
        return None
    points = np.asarray(corners, np.float64).reshape(-1, 2)
    if np.any(points < 6) or np.any(points > np.array(image.shape[::-1]) - 7):
        return None
    # OpenCV resize maps pixel centres, not pixel edges. Preserve the full-resolution reference.
    points = (points + 0.5) * (np.array(size) / np.array(image.shape[::-1])) - 0.5
    return Observation(frame, time_s, points, sharpness)


def diverse(candidate: Observation, selected: list[Observation], size: tuple[int, int]) -> bool:
    """Reject repeated poses, including the checkerboard detector's possible 180-degree reversal."""
    scale = np.array(size)
    for old in selected:
        distance = min(
            float(np.sqrt(np.mean(((candidate.points - points) / scale) ** 2)))
            for points in (old.points, old.points[::-1])
        )
        if distance < 0.018:
            return False
    return True


def _fit(views: list[Observation], size: tuple[int, int], model: Model) -> tuple[float, Points, Points, Any]:
    objects = [object_points() for _ in views]
    images = [view.points for view in views]
    if model == "opencv_fisheye":
        # OpenCV 5 moved these constants out of the fisheye namespace.
        flags: Any = cv2.fisheye if hasattr(cv2.fisheye, "CALIB_FIX_SKEW") else cv2
        rms, matrix, coefficients, _, _ = cv2.fisheye.calibrate(
            [points.reshape(1, -1, 3) for points in objects],
            [points.reshape(1, -1, 2) for points in images],
            size,
            np.eye(3),
            np.zeros((4, 1)),
            flags=flags.CALIB_RECOMPUTE_EXTRINSIC | flags.CALIB_FIX_SKEW,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-8),
        )
        return float(rms), np.asarray(matrix), np.asarray(coefficients), None
    rms, matrix, coefficients, _, _, deviations, _, _ = cv2.calibrateCameraExtended(
        [points.astype(np.float32) for points in objects],
        [points.astype(np.float32) for points in images],
        size,
        np.eye(3),
        np.zeros((5, 1)),
    )
    uncertainty = [float(value) if np.isfinite(value) else None for value in deviations.ravel()]
    return float(rms), np.asarray(matrix), np.asarray(coefficients), uncertainty


def _residual(view: Observation, matrix: Points, coefficients: Points, model: Model) -> tuple[float, Points]:
    objects = object_points()
    points = view.points.reshape(-1, 1, 2)
    if model == "opencv_fisheye":
        normalised = cv2.fisheye.undistortPoints(points, matrix, coefficients)
        ok, rotation, translation = cv2.solvePnP(objects, normalised, np.eye(3), None)
        if not ok:
            raise ValueError("Could not validate a checkerboard pose")
        projected, _ = cv2.fisheye.projectPoints(
            objects.reshape(-1, 1, 3), rotation, translation, matrix, coefficients
        )
    else:
        ok, rotation, translation = cv2.solvePnP(objects, points, matrix, coefficients)
        if not ok:
            raise ValueError("Could not validate a checkerboard pose")
        projected, _ = cv2.projectPoints(objects, rotation, translation, matrix, coefficients)
    rms = float(np.sqrt(np.mean(np.sum((projected.reshape(-1, 2) - view.points) ** 2, axis=1))))
    normal = np.asarray(cv2.Rodrigues(rotation)[0])[:, 2]
    if normal[2] < 0:
        normal = -normal
    return rms, np.degrees(np.arctan2(normal[:2], normal[2]))


def solve(
    views: list[Observation], size: tuple[int, int], model: Model
) -> tuple[LensCalibration, dict[str, Any]]:
    if len(views) < MIN_VIEWS:
        raise ValueError(
            f"Only {len(views)} distinct sharp boards found; at least {MIN_VIEWS} are needed. "
            "Keep the entire checkerboard visible for more of each sweep, lock focus, and add tilted views."
        )
    if model not in ("opencv_standard", "opencv_fisheye"):
        raise ValueError("Choose an OpenCV standard or fisheye lens model")
    if any(view.points.shape != (54, 2) or not np.isfinite(view.points).all() for view in views):
        raise ValueError("Invalid checkerboard observations")
    # Held-out views never participate in fitting or outlier selection.
    held_out = views[::5]
    training = [view for i, view in enumerate(views) if i % 5]
    try:
        rms, matrix, coefficients, deviations = _fit(training, size, model)
        errors = [_residual(view, matrix, coefficients, model)[0] for view in training]
        cutoff = max(0.75, float(np.median(errors)) * 2.5)
        keep = [view for view, error in zip(training, errors, strict=True) if error <= cutoff]
        rejected = len(training) - len(keep)
        if rejected and len(keep) >= 15:
            training = keep
            rms, matrix, coefficients, deviations = _fit(training, size, model)
        else:
            rejected = 0
        measurements = [_residual(view, matrix, coefficients, model) for view in training]
        validation = [_residual(view, matrix, coefficients, model)[0] for view in held_out]
    except cv2.error as exc:
        raise ValueError(
            "Lens fit failed. Record more sharp views with different tilts and distances."
        ) from exc
    if not np.isfinite([rms, *matrix.ravel(), *coefficients.ravel(), *validation]).all():
        raise ValueError("The lens fit is not finite; record more varied views")
    points = np.concatenate([view.points for view in training]) / np.array(size)
    coverage = np.zeros((4, 6), dtype=bool)
    cells = np.clip((points * [6, 4]).astype(int), [0, 0], [5, 3])
    coverage[cells[:, 1], cells[:, 0]] = True
    spans = np.ptp(np.array([angles for _, angles in measurements]), axis=0)
    areas = [float(cv2.contourArea(cv2.convexHull(view.points.astype(np.float32)))) for view in training]
    scale_ratio = float(np.sqrt(max(areas) / max(min(areas), 1)))
    validation_rms = float(np.sqrt(np.mean(np.square(validation))))
    issues = []
    if rms > MAX_RMS_PX or validation_rms > MAX_RMS_PX:
        issues.append(
            "Reprojection error exceeds 1.5 recording pixels. Use a sharper image and slower movements."
        )
    if min(spans) < 12:
        issues.append(
            "More perspective variation is needed: add oblique views from above, below and both sides."
        )
    if scale_ratio < 1.3:
        issues.append("Repeat the sequence farther away so the board appears at least 30% smaller.")
    if float(coverage.mean()) < 0.65:
        issues.append("Cover more of the image edges and corners with the checkerboard.")
    if not (
        0.1 * size[0] < matrix[0, 2] < 0.9 * size[0]
        and 0.1 * size[1] < matrix[1, 2] < 0.9 * size[1]
        and 0.15 * size[0] < matrix[0, 0] < 8 * size[0]
        and 0.15 * size[1] < matrix[1, 1] < 8 * size[0]
    ):
        issues.append("The fitted focal length or optical centre is implausible. Add more oblique views.")
    # A fitted radial polynomial must stay monotonic over the observed radius.
    radius = np.linalg.norm((points * size - matrix[:2, 2]) / [matrix[0, 0], matrix[1, 1]], axis=1).max()
    r = np.linspace(0, radius, 100)
    d = coefficients.ravel()
    if model == "opencv_fisheye":
        theta = np.arctan(r)
        derivative = 1 + sum((2 * i + 3) * value * theta ** (2 * i + 2) for i, value in enumerate(d))
    else:
        derivative = 1 + 3 * d[0] * r**2 + 5 * d[1] * r**4 + 7 * d[4] * r**6
    if np.min(derivative) <= 0:
        issues.append("The fitted distortion folds inside the observed image area. Record more edge views.")
    lens = LensCalibration(
        model=model,
        width=size[0],
        height=size[1],
        original_width=size[0],
        original_height=size[1],
        camera_matrix=matrix.tolist(),
        coefficients=tuple(d),
        asymmetrical=True,
        rms_error=rms,
        image_count=len(training),
    )
    per_view = [
        {
            "frame": view.frame,
            "time_s": view.time_s,
            "rms_px": error,
            "sharpness": view.sharpness,
            "tilt_degrees": angles.tolist(),
            "board_bounds_normalized": [
                (view.points.min(axis=0) / size).tolist(),
                (view.points.max(axis=0) / size).tolist(),
            ],
        }
        for view, (error, angles) in zip(training, measurements, strict=True)
    ]
    return lens, {
        "passed": not issues,
        "issues": issues,
        "training_views": len(training),
        "held_out_views": len(held_out),
        "outlier_views": rejected,
        "rms_px": rms,
        "held_out_rms_px": validation_rms,
        "held_out_errors_px": validation,
        "coverage_grid": coverage.tolist(),
        "coverage_fraction": float(coverage.mean()),
        "tilt_span_degrees": spans.tolist(),
        "distance_scale_ratio": scale_ratio,
        "intrinsic_standard_deviations": deviations,
        "intrinsic_standard_deviation_order": [
            "fx",
            "fy",
            "cx",
            "cy",
            "k1",
            "k2",
            "p1",
            "p2",
            "k3",
            "k4",
            "k5",
            "k6",
            "s1",
            "s2",
            "s3",
            "s4",
            "tau_x",
            "tau_y",
        ]
        if deviations is not None
        else None,
        "per_view": per_view,
        "thresholds": {
            "max_rms_px": MAX_RMS_PX,
            "min_tilt_span_degrees": 12,
            "min_distance_scale_ratio": 1.3,
            "min_coverage_fraction": 0.65,
        },
    }
