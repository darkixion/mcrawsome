"""DNG 1.7.1.0 chapter 6: actual camera, reference camera and XYZ colour transforms for stills."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from mcrawsome.colour.robertson import uv_to_cct
from mcrawsome.colour.spaces import ACES_WHITE, AP1_TO_XYZ, D50, bradford, bradford_xyz, xy_to_xyz
from mcrawsome.io.metadata import ContainerMetadata

Matrix = npt.NDArray[np.float64]
# EXIF LightSource temperatures used for DNG's reciprocal-temperature interpolation.
ILLUMINANT_TEMPERATURES = {
    1: 5500, 2: 4230, 3: 2850, 4: 5500, 9: 5500, 10: 6500, 11: 7500,
    12: 6400, 13: 5050, 14: 4150, 15: 3525, 16: 2925, 17: 2850, 18: 4874,
    19: 6774, 20: 5500, 21: 6500, 22: 7500, 23: 5000, 24: 3200,
}  # fmt: skip


def temperature(xy: tuple[float, float]) -> float:
    """Robertson's isotemperature lines in CIE 1960 uv, as used by the DNG white-balance method."""
    x, y = xy
    denominator = -2 * x + 12 * y + 3
    return uv_to_cct(4 * x / denominator, 6 * y / denominator)[0]


def weight(meta: ContainerMetadata, cct: float) -> float:
    calibration = meta.dng_calibration
    assert calibration is not None
    a, b = (ILLUMINANT_TEMPERATURES.get(i, 5000) for i in calibration.illuminants)
    if a == b:
        return 1.0
    return float(np.clip((1 / cct - 1 / b) / (1 / a - 1 / b), 0, 1))


def matrices(meta: ContainerMetadata, w: float) -> tuple[Matrix, Matrix]:
    calibration = meta.dng_calibration
    assert calibration is not None
    cm = w * np.reshape(meta.colour_matrix_1, (3, 3)) + (1 - w) * np.reshape(meta.colour_matrix_2, (3, 3))
    cc1 = np.reshape(meta.calibration_matrix_1, (3, 3)) if meta.calibration_matrix_1 else np.eye(3)
    cc2 = np.reshape(meta.calibration_matrix_2, (3, 3)) if meta.calibration_matrix_2 else np.eye(3)
    individual = np.diag(calibration.analog_balance) @ (w * cc1 + (1 - w) * cc2)
    return individual, individual @ cm


def camera_to_ap1(
    meta: ContainerMetadata, neutral: tuple[float, float, float]
) -> tuple[npt.NDArray[np.float32], float]:
    calibration = meta.dng_calibration
    assert calibration is not None
    n = np.asarray(neutral, np.float64)
    xy = D50
    for _ in range(100):
        w = weight(meta, temperature(xy))
        individual, camera = matrices(meta, w)
        xyz = np.linalg.solve(camera, n)
        next_xy = (float(xyz[0] / xyz.sum()), float(xyz[1] / xyz.sum()))
        if max(abs(next_xy[i] - xy[i]) for i in (0, 1)) < 1e-10:
            xy = next_xy
            break
        xy = next_xy
    cct = temperature(xy)
    w = weight(meta, cct)
    individual, camera = matrices(meta, w)
    has1, has2 = calibration.forward_matrices
    if has1 or has2:
        fm1 = np.reshape(meta.forward_matrix_1 if has1 else meta.forward_matrix_2, (3, 3))
        fm2 = np.reshape(meta.forward_matrix_2 if has2 else meta.forward_matrix_1, (3, 3))
        reference = np.linalg.solve(individual, n)
        to_xyz = (w * fm1 + (1 - w) * fm2) @ np.diag(1 / reference) @ np.linalg.inv(individual)
    else:
        to_xyz = bradford_xyz(xy_to_xyz(xy), xy_to_xyz(D50)) @ np.linalg.inv(camera)
        to_xyz /= (to_xyz @ n)[1]
    result = np.linalg.inv(AP1_TO_XYZ) @ bradford(D50, ACES_WHITE) @ to_xyz @ np.diag(n)
    return result.astype(np.float32), cct
