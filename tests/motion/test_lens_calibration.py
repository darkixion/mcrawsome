"""Measured lens geometry must survive interchange without turning into a pinhole."""

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mcrawsome.camera.profile import ProfileError, load_profile, save_profile
from mcrawsome.motion.gyroflow import import_gyroflow_profile, lens_profile


def calibration(model: str = "opencv_fisheye") -> dict[str, Any]:
    return {
        "calib_dimension": {"w": 4000, "h": 3000},
        "orig_dimension": {"w": 4000, "h": 3000},
        "distortion_model": model,
        "asymmetrical": True,
        "crop": 1.05,
        "num_images": 23,
        "input_horizontal_stretch": 1.0,
        "input_vertical_stretch": 1.0,
        "fisheye_params": {
            "camera_matrix": [[3100.0, 0.0, 1980.0], [0.0, 3090.0, 1480.0], [0.0, 0.0, 1.0]],
            "distortion_coeffs": [0.01, -0.02, 0.003, 0.004],
            "radial_distortion_limit": 1.2,
            "RMS_error": 0.4,
        },
    }


@pytest.mark.parametrize("model", ["opencv_standard", "opencv_fisheye"])
def test_measured_geometry_survives_save_load_and_export(
    model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    source = calibration(model)
    profile = import_gyroflow_profile(source, 4000, 3000)
    saved = load_profile(save_profile(profile))
    # A gyro fit may revise its scalar focal length; it must not overwrite measured intrinsics.
    saved = saved.model_copy(update={"focal_px": 1000.0})
    result = lens_profile(saved, (4000, 3000), 24.0)
    assert result["distortion_model"] == model
    assert result["fisheye_params"]["camera_matrix"] == source["fisheye_params"]["camera_matrix"]
    expected = source["fisheye_params"]["distortion_coeffs"]
    coefficients = result["fisheye_params"]["distortion_coeffs"]
    assert coefficients[: len(expected)] == expected
    assert not any(coefficients[len(expected) :])
    assert result["fisheye_params"]["radial_distortion_limit"] == 1.2
    assert result["asymmetrical"] is True
    assert result["crop"] == 1.05
    assert result["fisheye_params"]["RMS_error"] == 0.4
    assert result["num_images"] == 23
    assert "measured lens" in result["note"]


def test_uniform_resize_preserves_asymmetric_principal_point() -> None:
    source = calibration()
    profile = import_gyroflow_profile(source, 2000, 1500)
    result = lens_profile(profile, (2000, 1500), 24.0)
    np.testing.assert_allclose(
        result["fisheye_params"]["camera_matrix"],
        [[1550.0, 0.0, 990.0], [0.0, 1545.0, 740.0], [0.0, 0.0, 1.0]],
    )
    assert result["calib_dimension"] == {"w": 2000, "h": 1500}
    assert result["orig_dimension"] == {"w": 2000, "h": 1500}
    assert profile.focal_px == pytest.approx(3095 / 1.05 / 2)
    assert result["fisheye_params"]["RMS_error"] == 0.2


def test_unknown_crop_does_not_silently_become_a_calibrated_export() -> None:
    profile = import_gyroflow_profile(calibration(), 3840, 2160)
    assert profile.lens_calibration is not None  # original calibration is still retained
    assert profile.lens_calibration.width == 4000
    with pytest.raises(ProfileError, match=r"aspect ratio.*crop"):
        lens_profile(profile, (3840, 2160), 24.0)


def test_omitted_model_uses_gyroflows_fisheye_default() -> None:
    source = calibration()
    del source["distortion_model"]
    imported = import_gyroflow_profile(source, 4000, 3000)
    assert imported.lens_calibration is not None
    assert imported.lens_calibration.model == "opencv_fisheye"


@pytest.mark.parametrize("count", [4, 5, 8, 12])
def test_standard_model_keeps_every_supported_coefficient(count: int) -> None:
    source = calibration("opencv_standard")
    source["fisheye_params"]["distortion_coeffs"] = [i / 1000 for i in range(count)]
    result = lens_profile(import_gyroflow_profile(source, 4000, 3000), (4000, 3000), 24.0)
    assert result["fisheye_params"]["distortion_coeffs"] == (
        source["fisheye_params"]["distortion_coeffs"] + [0.0] * (12 - count)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distortion_model", "gopro"),
        ("digital_lens", "gopro_superview"),
        ("interpolations", {"100": {"crop": 1.1}}),
        ("crop", float("nan")),
        ("input_horizontal_stretch", 0.0),
        ("calib_dimension", {"w": 0, "h": 3000}),
        ("calib_dimension", {"w": 4000, "h": float("inf")}),
    ],
)
def test_unsupported_or_nonfinite_geometry_is_a_profile_error(field: str, value: Any) -> None:
    source = calibration()
    source[field] = value
    with pytest.raises(ProfileError):
        import_gyroflow_profile(source, 4000, 3000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("camera_matrix", [[float("nan"), 0, 2000], [0, 3000, 1500], [0, 0, 1]]),
        ("camera_matrix", [[3100, 2, 2000], [0, 3000, 1500], [0, 0, 1]]),
        ("camera_matrix", [[3100, 0, 2000], [0, 3000, 1500], [0, 0, 0]]),
        ("camera_matrix", [[3100, 0, 2000], [0, 3000, 1500]]),
        ("distortion_coeffs", [float("inf"), 0, 0, 0]),
        ("distortion_coeffs", [0.1, 0.2, 0.3]),
        ("radial_distortion_limit", -1.0),
    ],
)
def test_malformed_parameters_are_a_profile_error(field: str, value: Any) -> None:
    source = copy.deepcopy(calibration())
    source["fisheye_params"][field] = value
    with pytest.raises(ProfileError):
        import_gyroflow_profile(source, 4000, 3000)


@pytest.mark.parametrize("source", [[], None, "lens", {"fisheye_params": []}])
def test_non_object_input_is_a_profile_error(source: Any) -> None:
    with pytest.raises(ProfileError):
        import_gyroflow_profile(source, 4000, 3000)


@pytest.mark.parametrize("coefficients", [None, []])
def test_focal_only_import_does_not_claim_measured_distortion(coefficients: Any) -> None:
    source = calibration()
    if coefficients is None:
        del source["fisheye_params"]["distortion_coeffs"]
    else:
        source["fisheye_params"]["distortion_coeffs"] = coefficients
    imported = import_gyroflow_profile(source, 4000, 3000)
    assert imported.lens_calibration is None
    assert imported.focal_px == pytest.approx(3095 / 1.05)


def test_stretch_and_original_dimensions_are_retained() -> None:
    source = calibration()
    source["input_horizontal_stretch"] = 1.1
    source["input_vertical_stretch"] = 1.2
    source["orig_dimension"] = {"w": 2000, "h": 1500}
    imported = import_gyroflow_profile(source, 4000, 3000)
    assert imported.lens_calibration is not None
    assert imported.lens_calibration.original_width == 2000
    result = lens_profile(imported, (4000, 3000), 24.0)
    assert result["input_horizontal_stretch"] == 1.1
    assert result["input_vertical_stretch"] == 1.2
