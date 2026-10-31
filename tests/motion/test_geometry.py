from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.motion.geometry import (
    quat_conjugate,
    quat_from_rotvec,
    quat_mul,
    quat_to_matrix,
    smooth_quats,
    warp_maps,
)

DATA = Path(__file__).parent.parent / "data"


def test_smoothing_matches_prototype() -> None:
    ref = np.load(DATA / "ref_motion.npz")
    np.testing.assert_allclose(smooth_quats(ref["q"], 12.0), ref["q_smooth"], atol=1e-9)


def test_warp_maps_match_prototype() -> None:
    ref = np.load(DATA / "ref_motion.npz")
    for bands, mx, my in zip(ref["bands"], ref["map_x"], ref["map_y"], strict=True):
        got_x, got_y = warp_maps(bands, focal=190.0, zoom=1.1, width=240, height=135)
        np.testing.assert_allclose(got_x, mx, atol=1e-3)
        np.testing.assert_allclose(got_y, my, atol=1e-3)


@given(st.lists(st.floats(-1.0, 1.0), min_size=3, max_size=3))
def test_rotation_matrices_are_orthonormal(rotvec: list[float]) -> None:
    r = quat_to_matrix(quat_from_rotvec(np.array(rotvec)))
    np.testing.assert_allclose(r @ r.T, np.eye(3), atol=1e-9)
    assert np.linalg.det(r) > 0


def test_conjugate_undoes_a_rotation() -> None:
    q = quat_from_rotvec(np.array([0.1, -0.2, 0.3]))
    np.testing.assert_allclose(quat_mul(q, quat_conjugate(q)), [1, 0, 0, 0], atol=1e-12)


def test_smoothing_a_constant_track_changes_nothing() -> None:
    q = np.repeat(quat_from_rotvec(np.array([0.05, 0.0, -0.02]))[None], 50, 0)
    np.testing.assert_allclose(smooth_quats(q, 5.0), q, atol=1e-12)


def test_identity_rotation_gives_identity_maps() -> None:
    mx, my = warp_maps(np.repeat(np.eye(3)[None], 4, 0), focal=500.0, zoom=1.0, width=64, height=36)
    yy, xx = np.mgrid[0:36, 0:64]
    np.testing.assert_allclose(mx, xx, atol=1e-4)
    np.testing.assert_allclose(my, yy, atol=1e-4)


def test_yaw_moves_the_centre_by_focal_tan_angle() -> None:
    angle = 0.02
    r = quat_to_matrix(quat_from_rotvec(np.array([0.0, angle, 0.0])))
    mx, _ = warp_maps(np.repeat(r[None], 4, 0), focal=1000.0, zoom=1.0, width=2000, height=1000)
    assert abs(abs(mx[500, 1000] - 1000) - 1000 * np.tan(angle)) < 0.05


def test_warp_map_scratch_memory_is_bounded() -> None:
    import tracemalloc

    bands = quat_to_matrix(quat_from_rotvec(np.full((12, 3), 0.01)))
    tracemalloc.start()
    try:
        mx, my = warp_maps(bands, focal=750.0, zoom=1.1, width=1024, height=768)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < mx.nbytes + my.nbytes + 10_000_000


@pytest.mark.parametrize(("width", "height"), [(240, 135), (135, 240), (257, 385)])
def test_warp_maps_keep_full_precision_across_row_boundaries(width: int, height: int) -> None:
    from mcrawsome.motion.geometry import intrinsics, zoom_matrix

    bands = quat_to_matrix(quat_from_rotvec(np.random.default_rng(8).normal(0, 0.02, (12, 3))))
    k = intrinsics(750.0, width, height)
    inverse = np.linalg.inv(zoom_matrix(1.1, width, height) @ k @ bands @ np.linalg.inv(k))
    _yy, xx = np.mgrid[:height, :width].astype(np.float64)
    rows = np.arange(height, dtype=np.float64)
    band_rows = (np.arange(len(bands)) + 0.5) / len(bands) * height
    per_row = np.stack(
        [np.stack([np.interp(rows, band_rows, inverse[:, i, j]) for j in range(3)], -1) for i in range(3)], -2
    )
    den = per_row[:, 2, 0, None] * xx + (per_row[:, 2, 1] * rows + per_row[:, 2, 2])[:, None]
    expected = [
        ((per_row[:, c, 0, None] * xx + (per_row[:, c, 1] * rows + per_row[:, c, 2])[:, None]) / den).astype(
            np.float32
        )
        for c in (0, 1)
    ]
    for actual, reference in zip(warp_maps(bands, 750.0, 1.1, width, height), expected, strict=True):
        np.testing.assert_array_equal(actual, reference)
