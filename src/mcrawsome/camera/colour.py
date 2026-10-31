"""Camera RGB to ACEScg (AP1) using the DNG-style matrices MotionCam records."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from mcrawsome.colour.spaces import ACES_WHITE, AP1, D50, bradford, rgb_to_xyz, xy_to_xyz
from mcrawsome.io.metadata import ContainerMetadata

_T1, _T2 = 2856.0, 6504.0  # illuminant A and D65


def _mat(values: list[float]) -> npt.NDArray[np.float64]:
    return np.array(values, dtype=np.float64).reshape(3, 3)


def _xy_to_cct(x: float, y: float) -> float:
    n = (x - 0.3320) / (0.1858 - y)
    return float(449 * n**3 + 3525 * n**2 + 6823.3 * n + 5520.33)


def _weight(cct: float) -> float:
    return float(np.clip((1 / cct - 1 / _T2) / (1 / _T1 - 1 / _T2), 0.0, 1.0))


def with_colour_matrix(meta: ContainerMetadata, matrix: tuple[float, ...]) -> ContainerMetadata:
    """The camera's metadata with a colour matrix override (XYZ to camera) for both illuminants, and its
    inverse as the forward matrix, so development matches DNGs written with the override."""
    inverse = np.linalg.inv(_mat(list(matrix))).ravel().tolist()
    return meta.model_copy(
        update={
            "colour_matrix_1": list(matrix),
            "colour_matrix_2": list(matrix),
            "forward_matrix_1": inverse,
            "forward_matrix_2": inverse,
        }
    )


def native_to_ap1(matrix: tuple[float, ...]) -> npt.NDArray[np.float64]:
    """White-balanced camera RGB (a Camera Native video) to AP1 through a colour matrix (XYZ to camera, as
    DNG ColorMatrix): camera white lands on ACES white, as `camera_to_ap1` maps the as-shot neutral."""
    fm = np.linalg.inv(_mat(list(matrix)))
    fm = fm * (xy_to_xyz(D50) / (fm @ np.ones(3)))[:, None]  # camera white -> D50 white exactly
    return np.asarray(np.linalg.inv(rgb_to_xyz(AP1, ACES_WHITE)) @ bradford(D50, ACES_WHITE) @ fm, np.float64)


def camera_to_ap1(
    meta: ContainerMetadata, neutral: tuple[float, float, float]
) -> tuple[npt.NDArray[np.float32], float]:
    """Interpolate the matrices by the CCT of the as-shot neutral (DNG method)."""
    if meta.dng_calibration is not None:
        from mcrawsome.camera.dng_colour import camera_to_ap1 as dng_to_ap1

        return dng_to_ap1(meta, neutral)
    cm1, cm2 = _mat(meta.colour_matrix_1), _mat(meta.colour_matrix_2)
    fm1, fm2 = _mat(meta.forward_matrix_1), _mat(meta.forward_matrix_2)
    n = np.asarray(neutral, dtype=np.float64)
    cct = 5000.0
    for _ in range(20):
        w = _weight(cct)
        xyz = np.linalg.solve(w * cm1 + (1 - w) * cm2, n)
        cct = _xy_to_cct(xyz[0] / xyz.sum(), xyz[1] / xyz.sum())
    w = _weight(cct)
    fm = w * fm1 + (1 - w) * fm2
    fm = fm * (xy_to_xyz(D50) / (fm @ np.ones(3)))[:, None]  # camera white -> D50 white exactly
    m = np.linalg.inv(rgb_to_xyz(AP1, ACES_WHITE)) @ bradford(D50, ACES_WHITE) @ fm
    return m.astype(np.float32), cct
