"""Portable, bounded lens-shading overrides; the recording's metadata stays unchanged."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from mcrawsome.io.metadata import ContainerMetadata, FrameMetadata

Gain = Annotated[FiniteFloat, Field(gt=0, le=256)]
GainPlane = Annotated[tuple[Gain, ...], Field(min_length=4, max_length=128 * 128)]


class VignetteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    sensor_width: int = Field(gt=0, le=65536)
    sensor_height: int = Field(gt=0, le=65536)
    cfa: Literal["rggb", "bggr", "grbg", "gbrg"]
    map_width: int = Field(ge=2, le=128)
    map_height: int = Field(ge=2, le=128)
    gains: tuple[GainPlane, ...] = Field(min_length=4, max_length=4)
    device_model: str = Field(default="", max_length=256)
    camera_id: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def dimensions(self) -> Self:
        if any(len(plane) != self.map_width * self.map_height for plane in self.gains):
            raise ValueError("gain planes must match the map dimensions")
        return self


def capture_profile(container: ContainerMetadata, frame: FrameMetadata) -> VignetteProfile:
    return VignetteProfile(
        sensor_width=frame.original_width, sensor_height=frame.original_height,
        cfa=container.sensor_arrangement, map_width=frame.lens_shading_map_width,
        map_height=frame.lens_shading_map_height,
        gains=tuple(tuple(plane) for plane in frame.lens_shading_map),
        device_model=container.device_model or "", camera_id=container.camera_id or "",
    )  # fmt: skip


def apply_profile(
    container: ContainerMetadata, frame: FrameMetadata, profile: VignetteProfile | None
) -> FrameMetadata:
    if profile is None:
        return frame
    if (profile.sensor_width, profile.sensor_height, profile.cfa) != (
        frame.original_width,
        frame.original_height,
        container.sensor_arrangement,
    ):
        raise ValueError("Vignette profile needs the same sensor dimensions and Bayer pattern")
    return frame.model_copy(update={
        "lens_shading_map_width": profile.map_width, "lens_shading_map_height": profile.map_height,
        "lens_shading_map": [list(plane) for plane in profile.gains],
    })  # fmt: skip
