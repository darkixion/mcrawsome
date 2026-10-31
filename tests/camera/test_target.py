"""Numerical recovery, independent validation and actual checkerboard detection."""

import cv2
import numpy as np
import pytest

from mcrawsome.camera.target import Model, Observation, detect, diverse, object_points, solve

SIZE = (2048, 1536)
MATRIX = np.array([[1450.0, 0, 1020], [0, 1445.0, 760], [0, 0, 1]])


def views(model: Model = "opencv_standard", *, noise: float = 0.08) -> list[Observation]:
    rng = np.random.default_rng(18)
    coefficients = np.array([-0.09, 0.025, 0.001, -0.001, -0.002])
    if model == "opencv_fisheye":
        coefficients = np.array([0.08, -0.025, 0.003, -0.0002])
    result = []
    for i in range(70):
        rotation = rng.uniform([-0.45, -0.5, -0.2], [0.45, 0.5, 0.2])
        translation = np.array([-4, -2.5, rng.uniform(9, 19)]) + rng.uniform([-2, -2, 0], [2, 2, 0])
        if model == "opencv_fisheye":
            projected, _ = cv2.fisheye.projectPoints(
                object_points().reshape(-1, 1, 3), rotation, translation, MATRIX, coefficients
            )
        else:
            projected, _ = cv2.projectPoints(object_points(), rotation, translation, MATRIX, coefficients)
        points = projected.reshape(-1, 2)
        if np.any(points < 8) or np.any(points > np.array(SIZE) - 9):
            continue
        result.append(Observation(i, i / 3, points + rng.normal(0, noise, points.shape), 150))
    return result


@pytest.mark.parametrize("model", ["opencv_standard", "opencv_fisheye"])
def test_recovers_lens_geometry_and_validates_independent_views(model: Model) -> None:
    lens, quality = solve(views(model), SIZE, model)
    assert lens.camera_matrix[0][0] == pytest.approx(MATRIX[0, 0], rel=0.01)
    assert lens.camera_matrix[1][1] == pytest.approx(MATRIX[1, 1], rel=0.01)
    assert lens.camera_matrix[0][2] == pytest.approx(MATRIX[0, 2], abs=4)
    assert quality["held_out_rms_px"] < 0.2
    assert quality["training_views"] == lens.image_count
    assert quality["held_out_views"] >= 4
    assert quality["passed"], quality["issues"]
    held_out_frames = {view.frame for view in views(model)[::5]}
    assert not held_out_frames.intersection(view["frame"] for view in quality["per_view"])


def test_poor_validation_cannot_be_hidden_by_a_good_training_error() -> None:
    source = views()
    rng = np.random.default_rng(2)
    source[::5] = [
        Observation(v.frame, v.time_s, v.points + rng.normal(0, 4, v.points.shape)) for v in source[::5]
    ]
    _, quality = solve(source, SIZE, "opencv_standard")
    assert quality["rms_px"] < 0.2
    assert not quality["passed"]
    assert quality["held_out_rms_px"] > 3
    assert any("Reprojection" in issue for issue in quality["issues"])


def test_rejects_too_few_views_and_does_not_count_reversed_duplicates() -> None:
    source = views()
    with pytest.raises(ValueError, match="at least 20"):
        solve(source[:10], SIZE, "opencv_standard")
    assert not diverse(source[0], [source[0]], SIZE)
    reversed_view = Observation(90, 30, source[0].points[::-1])
    assert not diverse(reversed_view, [source[0]], SIZE)
    assert diverse(source[-1], [source[0]], SIZE)


def test_detects_real_square_board_and_maps_pixel_centres_back_to_recording() -> None:
    image = np.full((720, 960), 255, np.uint8)
    for row in range(7):
        for column in range(10):
            if (row + column) % 2 == 0:
                image[80 + row * 70 : 80 + (row + 1) * 70, 100 + column * 70 : 100 + (column + 1) * 70] = 0
    view = detect(image, (1920, 1440), 15, 0.5)
    assert view is not None
    assert view.points.shape == (54, 2)
    # Internal corner extremes: detector uses half-pixel centres at square boundaries.
    assert view.points[:, 0].min() == pytest.approx(339.5, abs=0.2)
    assert view.points[:, 0].max() == pytest.approx(1459.5, abs=0.2)
    assert detect(np.full_like(image, 128), SIZE, 0, 0) is None
