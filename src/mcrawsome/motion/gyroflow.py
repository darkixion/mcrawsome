"""Gyroflow interchange: lens profiles out and in, and a .gcsv with the fitted sync applied.

Gyroflow's lens profile is a JSON file (the fields of its LensProfile struct). Imported measured lens
geometry is retained for interchange, separately from the gyro fit's scalar focal length. A profile
without a measured lens still writes an explicitly uncalibrated pinhole. Pixel correction is separate.
"""

from __future__ import annotations

import datetime
import math
from typing import Any

import numpy as np
from pydantic import ValidationError

from mcrawsome import __version__
from mcrawsome.camera.lens import LensCalibration
from mcrawsome.camera.profile import CameraProfile, ProfileError
from mcrawsome.io.gyro import GyroLog

_COEFFICIENTS = 12
_TSCALE, _GSCALE, _ASCALE = 1e-6, 1e-9, 1e-9


def lens_profile(
    profile: CameraProfile,
    size: tuple[int, int],
    fps: float,
    camera_brand: str = "",
    camera_model: str = "",
) -> dict[str, Any]:
    """A Gyroflow lens profile for frames of `size` (width, height) from a profile with a focal length."""
    lens = profile.lens_calibration
    if profile.focal_px is None and lens is None:
        raise ProfileError(f"{profile.name}: no focal length, so no lens profile can be written")
    width, height = size
    if width <= 0 or height <= 0:
        raise ProfileError("the output dimensions must be positive")
    if profile.focal_px is not None:
        focal = float(profile.focal_px)
    else:
        assert lens is not None
        focal = 0.5 * (lens.camera_matrix[0][0] + lens.camera_matrix[1][1])
    matrix = [[focal, 0.0, width / 2], [0.0, focal, height / 2], [0.0, 0.0, 1.0]]
    coefficients = [0.0] * _COEFFICIENTS
    if lens is not None:
        try:
            matrix = lens.matrix_at(width, height)
        except ValueError as exc:
            raise ProfileError(f"{profile.name}: {exc}") from None
        coefficients = list(lens.coefficients)
        if lens.model == "opencv_standard":
            coefficients += [0.0] * (_COEFFICIENTS - len(coefficients))
        focal = (0.5 * matrix[0][0] + 0.5 * matrix[1][1]) / (lens.crop or 1.0)
    return {
        "name": f"{camera_brand} {camera_model} {width}x{height} (MCRAWsome)".strip(),
        "note": (
            "Written by MCRAWsome: measured lens geometry; image pixels have not been lens-corrected"
            if lens
            else "Written by MCRAWsome: focal length and readout only, no lens distortion"
        ),
        "calibrated_by": "MCRAWsome",
        "camera_brand": camera_brand,
        "camera_model": camera_model,
        "lens_model": f"{profile.name}, {focal:.0f} px",
        "camera_setting": "",
        "calib_dimension": {"w": width, "h": height},
        "orig_dimension": {"w": width, "h": height},
        "output_dimension": {"w": width, "h": height},
        "frame_readout_time": profile.readout_ms,
        "gyro_lpf": None,
        "input_horizontal_stretch": lens.horizontal_stretch if lens else 1.0,
        "input_vertical_stretch": lens.vertical_stretch if lens else 1.0,
        "num_images": lens.image_count if lens else 0,
        "fps": fps,
        "crop": lens.crop if lens else None,
        "official": False,
        "asymmetrical": lens.asymmetrical if lens else False,
        "fisheye_params": {
            "RMS_error": (lens.rms_error or 0.0) * width / lens.width if lens else 0.0,
            "camera_matrix": matrix,
            "distortion_coeffs": coefficients,
            "radial_distortion_limit": lens.radial_distortion_limit if lens else None,
        },
        "identifier": "",
        "calibrator_version": f"MCRAWsome {__version__}",
        "date": datetime.date.today().isoformat(),
        "compatible_settings": [],
        "sync_settings": None,
        "distortion_model": lens.model if lens else "opencv_standard",
        "global_shutter": False,
    }


def import_gyroflow_profile(data: dict[str, Any], width: int, height: int) -> CameraProfile:
    """The measured geometry and scalar focal/readout of a Gyroflow lens profile.

    The focal length is scaled by width, assuming the clip spans the calibration's full width (phone
    video may crop a 4:3 sensor). The original geometry is kept in its calibration coordinates; a
    changed aspect ratio cannot safely export it without a crop mapping. Axes and sync still come
    from each clip's fit. The caller sets the name and match. No lens correction is applied to pixels.
    """
    try:
        if not isinstance(data, dict):
            raise TypeError
        if data.get("digital_lens") or data.get("interpolations") or data.get("digital_lens_params"):
            raise ProfileError("digital lenses and interpolated lens calibrations are not supported")
        params, dimensions = data["fisheye_params"], data["calib_dimension"]
        original = data.get("orig_dimension") or dimensions
        model = data.get("distortion_model") or "opencv_fisheye"
        measured = params.get("distortion_coeffs")
        lens = LensCalibration.model_validate(
            {
                "model": model,
                "width": dimensions["w"],
                "height": dimensions["h"],
                "original_width": original["w"],
                "original_height": original["h"],
                "camera_matrix": params["camera_matrix"],
                # Old focal-only profiles remain usable, but are never labelled measured calibrations.
                "coefficients": measured
                if measured is not None and measured != []
                else [0.0] * (4 if model == "opencv_fisheye" else _COEFFICIENTS),
                "crop": data.get("crop"),
                "asymmetrical": data.get("asymmetrical", False),
                "horizontal_stretch": data.get("input_horizontal_stretch", 1.0),
                "vertical_stretch": data.get("input_vertical_stretch", 1.0),
                "radial_distortion_limit": params.get("radial_distortion_limit"),
                "rms_error": params.get("RMS_error"),
                "image_count": data.get("num_images", 0),
            }
        )
    except ValidationError as exc:
        raise ProfileError(f"invalid Gyroflow lens geometry: {exc}") from None
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ProfileError("not a Gyroflow lens profile (no camera matrix and calibration size)") from None
    readout = data.get("frame_readout_time")
    camera = " ".join(str(data.get(k) or "") for k in ("camera_brand", "camera_model", "lens_model")).strip()
    if width <= 0 or height <= 0:
        raise ProfileError("the clip dimensions must be positive")
    focal = (0.5 * lens.camera_matrix[0][0] + 0.5 * lens.camera_matrix[1][1]) * (width / lens.width)
    focal /= lens.crop or 1.0
    if not math.isfinite(focal):
        raise ProfileError("invalid Gyroflow focal length")
    try:
        return CameraProfile(
            name="imported",
            description=f"{camera or data.get('name') or 'camera'}, from a Gyroflow lens profile",
            focal_px=focal,
            readout_ms=abs(float(readout)) if readout is not None else None,
            calibrated=f"imported from Gyroflow ({data.get('calibrated_by') or 'unknown calibrator'})",
            lens_calibration=lens if measured is not None and measured != [] else None,
        )
    except (ValueError, TypeError, OverflowError):
        raise ProfileError("invalid Gyroflow focal length or rolling-shutter readout") from None


def synced_gcsv(log: GyroLog, offset_s: float) -> str:
    """The log as a Gyroflow .gcsv whose times are moved earlier by `offset_s`.

    The header's start timestamp moves later by the same amount, so every sample keeps its wall-clock
    time. Values are written as integers with fine scales (microseconds, nanoradians per second).
    """
    header = {k: v for k, v in log.header.items() if k not in ("tscale", "gscale", "ascale", "timestamp")}
    lines = ["GYROFLOW IMU LOG", f"version,{header.pop('version', '1.2')}"]
    lines += [f"{key},{value}" for key, value in header.items() if key != "orientation"]
    lines.append(f"orientation,{log.orientation}")
    if log.start_epoch_s is not None:
        lines.append(f"timestamp,{round((log.start_epoch_s + offset_s) * 1000)}")
    lines += [f"tscale,{_TSCALE:.9f}", f"gscale,{_GSCALE:.10f}"]
    columns = [np.rint((log.times_s - offset_s) / _TSCALE), np.rint(log.gyro / _GSCALE).T]
    names = "t,gx,gy,gz"
    if log.accel is not None:
        lines.append(f"ascale,{_ASCALE:.10f}")
        columns.append(np.rint(log.accel / _ASCALE).T)
        names += ",ax,ay,az"
    lines.append(names)
    table = np.vstack(columns).T.astype(np.int64)
    lines += [",".join(str(v) for v in row) for row in table]
    return "\n".join(lines) + "\n"
