"""Measured lens geometry, retained separately from the stabiliser's fitted focal length.

This is calibration data, not a correction pass. Pixel warping still uses the existing pinhole model.
Dimensions and principal points refer to the calibration image, before any unknown sensor crop.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

Row = tuple[FiniteFloat, FiniteFloat, FiniteFloat]


class LensCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: Literal["opencv_standard", "opencv_fisheye"]
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    original_width: int = Field(gt=0, strict=True)
    original_height: int = Field(gt=0, strict=True)
    camera_matrix: tuple[Row, Row, Row]
    coefficients: tuple[FiniteFloat, ...]
    crop: FiniteFloat | None = Field(None, gt=0)
    asymmetrical: bool = False
    horizontal_stretch: FiniteFloat = Field(1.0, gt=0)
    vertical_stretch: FiniteFloat = Field(1.0, gt=0)
    radial_distortion_limit: FiniteFloat | None = Field(None, gt=0)
    rms_error: FiniteFloat | None = Field(None, ge=0)
    image_count: int = Field(0, ge=0, strict=True)

    @model_validator(mode="after")
    def valid_geometry(self) -> Self:
        k = self.camera_matrix
        if k[0][0] <= 0 or k[1][1] <= 0 or k[0][1] != 0 or k[1][0] != 0 or k[2] != (0.0, 0.0, 1.0):
            raise ValueError(
                "camera matrix must have positive focal lengths, zero skew and a [0, 0, 1] last row"
            )
        lengths = (4,) if self.model == "opencv_fisheye" else (4, 5, 8, 12)
        if len(self.coefficients) not in lengths:
            raise ValueError(f"{self.model} needs {'/'.join(map(str, lengths))} distortion coefficients")
        return self

    def matrix_at(self, width: int, height: int) -> list[list[float]]:
        """Uniform resize only. An aspect change needs a measured or explicitly supplied crop mapping."""
        if width <= 0 or height <= 0:
            raise ValueError("the output dimensions must be positive")
        if (
            width * self.height != height * self.width
            or width * self.original_height != height * self.original_width
        ):
            raise ValueError(
                "calibration aspect ratio differs from the clip; the sensor crop mapping is unknown"
            )
        scale = width / self.width
        return [[float(v * scale) for v in row] for row in self.camera_matrix[:2]] + [[0.0, 0.0, 1.0]]
