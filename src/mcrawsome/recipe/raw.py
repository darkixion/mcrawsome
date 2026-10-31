"""RAW calibration shared with lightweight mounted-media services."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def number_list(value: Any) -> Any:
    """`64,64,64,66` from the command line, or a TOML list, as a tuple of numbers; anything else as given."""
    if isinstance(value, str) and "," in value:
        return tuple(part.strip() for part in value.split(","))
    if isinstance(value, list):
        return tuple(value)
    return value


if TYPE_CHECKING:
    from mcrawsome.io.metadata import FrameMetadata


Level = Annotated[float, Field(ge=0, le=65535)]


class RawCalibration(BaseModel):
    """RAW level and gain-map settings shared by development and immutable virtual media."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rotation: Literal[0, 90, 180, 270] = Field(
        0,
        description=(
            "RAW only: clockwise display turn after the recorded orientation; sensor pixels stay unchanged"
        ),
        json_schema_extra={"layer": 2},
    )

    lens_shading: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Lens vignette correction (1 full, 0 keeps the vignette, 0.5 softens it)",
        json_schema_extra={"layer": 3},
    )
    lens_shading_mode: Literal["full", "colour", "vignette"] = Field(
        "full",
        description=(
            "RAW only: full corrects the vignette and the colour shading; colour corrects the colour shading "
            "only, with no gain above 1 (keeps the highlight range and the vignette); vignette corrects the "
            "falloff only, leaving colours as recorded"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_shading: Literal["opcode", "baked"] = Field(
        "opcode",
        description=(
            "Virtual CinemaDNG only: opcode keeps the lens shading as DNG gain maps; baked applies it to the "
            "raw values as each frame is read, for editors such as DaVinci Resolve that show the vignette"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_proxy: Literal["full", "half", "quarter", "eighth"] = Field(
        "full",
        description=(
            "Virtual CinemaDNG only: full size, or proxies at a half, quarter or eighth of the width and "
            "height (each Bayer site averaged) for smoother editing"
        ),
        json_schema_extra={"layer": 3},
    )
    cfa_layout: Literal["auto", "bayer", "quad"] = Field(
        "auto",
        description=(
            "RAW only: how the colour filter is read. auto finds Quad Bayer from the recording's flag or its "
            "pattern; bayer always reads the recorded Bayer mosaic; quad always remosaics 2x2 colour cells "
            "(for recordings whose Quad Bayer flag is missing or wrong)"
        ),
        json_schema_extra={"layer": 3},
    )
    cfa_pattern: Literal["recorded", "rggb", "bggr", "grbg", "gbrg"] = Field(
        "recorded",
        description=(
            "RAW only: the Bayer order of the top-left 2x2 sites, replacing the recording's when its "
            "metadata is wrong (colours swap or turn magenta and green)"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_scale: bool = Field(
        False,
        description=(
            "Virtual CinemaDNG only: scale the served values so the white level is 65535, with the black "
            "levels in proportion, for editors that read low white levels poorly; linear mounts only"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_first_frame: Literal["proxy", "full"] = Field(
        "proxy",
        description=(
            "Virtual CinemaDNG proxies only: full serves the first frame at full size, so an editor that "
            "takes a sequence's size from its first frame lists the clip at its recorded size"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_frame_rate: (
        Literal["keep", "integer", "drop-frame", "average", "median"] | Annotated[float, Field(ge=1, le=1000)]
    ) = Field(
        "keep",
        description=(
            "Virtual CinemaDNG only: keep (the recorded rate, dropped frames filled), or a constant rate by "
            "repeating and dropping frames, as output.frame_rate; the sound stays at its recorded speed"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_normalise_exposure: Literal["off", "recorded", "picture"] = Field(
        "off",
        description=(
            "Virtual CinemaDNG only: recorded scales each frame to the first frame's recorded exposure (ISO "
            "times shutter) as it is read, with one stop of headroom; picture measures every frame's "
            "brightness once before mounting and evens out flicker and exposure steps towards the average "
            "over develop.mount_normalise_seconds, with up to two stops of headroom (true and false read as "
            "recorded and off)"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_normalise_seconds: float = Field(
        1.0,
        ge=0.1,
        le=60.0,
        description=(
            "Picture exposure normalisation on mounts: the window the brightness is averaged over (longer "
            "keeps only slower changes)"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_transfer: Literal["linear", "log"] = Field(
        "linear",
        description=(
            "Virtual CinemaDNG only: log serves log-encoded values with a linearisation table, as "
            "output.dng_transfer, for lighter editing (see develop.mount_reduce_bits)"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_reduce_bits: Literal["auto", 2, 4, 6, 8] = Field(
        "auto",
        description=(
            "Log mounts: bits removed from the served values, with dithering, as output.dng_reduce_bits "
            "(auto serves the sensor's bits less 2, allowing for a baked vignette's overhead)"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_crop: str = Field(
        "none",
        description=(
            "Virtual CinemaDNG only: auto removes a black strip on an edge (left by some 60 fps stream "
            "configurations), found in the first, middle and last frames when mounting; a size such as "
            "3840x2160 crops the recorded image from its top-left, and 3840x2160+64+32 places it 64 pixels "
            "from the left and 32 from the top, without changing sensor calibration"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_camera_model: str | None = Field(
        None,
        max_length=64,
        description=(
            "Virtual CinemaDNG only: the camera named in the DNGs, such as Panasonic Varicam RAW or "
            "Blackmagic Pocket Cinema Camera 4K, for other defaults in DaVinci Resolve's RAW tab (as "
            "output.dng_camera_model)"
        ),
        json_schema_extra={"layer": 3},
    )
    mount_unique_model: bool = Field(
        False,
        description="Virtual CinemaDNG only: add the clip's name to the camera model, so Resolve keeps each "
        "clip's RAW settings apart",
        json_schema_extra={"layer": 3},
    )
    level_policy: Literal["legacy", "recorded", "static", "explicit"] = Field(
        "legacy",
        description=(
            "RAW levels: legacy preserves existing partial overrides; recorded ignores fixed overrides "
            "and uses each frame's metadata; static uses frame 1 for every frame; explicit uses the "
            "black/white overrides, falling back per field to recorded metadata"
        ),
        json_schema_extra={"layer": 3},
    )
    camera_model_source: Literal["override", "recorded"] = Field(
        "override",
        description="Mounted camera name: recorded bypasses inherited camera-name overrides",
        json_schema_extra={"layer": 3},
    )

    black_level: Level | tuple[Level, Level, Level, Level] | None = Field(
        None,
        description=(
            "RAW only: black level replacing the clip's (fixes pink or green casts): one value, or four in "
            "Bayer order (64,64,64,66)"
        ),
        json_schema_extra={"layer": 3},
    )
    white_level: float | None = Field(
        None,
        ge=1,
        le=65535,
        description="RAW only: white (clipping) level replacing the clip's",
        json_schema_extra={"layer": 3},
    )

    def level_overrides(
        self, first: FrameMetadata
    ) -> tuple[float | tuple[float, float, float, float] | None, float | None]:
        """Resolve only this scoped policy; optional fields elsewhere keep their old semantics.

        Static always means the recording's first frame. Historical static snapshots have no policy
        and continue to use their saved values through legacy, without reinterpretation.
        """
        if self.level_policy == "recorded":
            return None, None
        if self.level_policy == "static":
            return first.black_level, first.white_level
        return self.black_level, self.white_level

    def serving(self, first: FrameMetadata) -> RawCalibration:
        """A fixed snapshot for consumers which already accept optional black/white overrides."""
        black, white = self.level_overrides(first)
        return self.model_copy(
            update={
                "black_level": black,
                "white_level": white,
                "mount_camera_model": None
                if self.camera_model_source == "recorded"
                else self.mount_camera_model,
            }
        )

    @field_validator("mount_crop")
    @classmethod
    def _crop_size(cls, value: str) -> str:
        from mcrawsome.develop.crop import parse_crop

        parse_crop(value)
        return value

    @field_validator("black_level", mode="before")
    @classmethod
    def _black_values(cls, value: Any) -> Any:
        return number_list(value)

    @field_validator("mount_normalise_exposure", mode="before")
    @classmethod
    def _normalise_switch(cls, value: Any) -> Any:
        """Settings saved before the picture mode stored a switch: on meant the recorded exposure."""
        return {True: "recorded", False: "off"}.get(value, value) if isinstance(value, bool) else value

    @field_validator("mount_reduce_bits", mode="before")
    @classmethod
    def _bits_number(cls, value: Any) -> Any:
        """--develop.mount_reduce_bits 4 arrives as text; the choices are numbers."""
        return int(value) if isinstance(value, str) and value.isdigit() else value


def saved_rotation(clip: Path, first_16mib: str | None = None) -> Literal[0, 90, 180, 270]:
    """Read only the display turn, independently of creative settings and look availability."""
    from mcrawsome.recipe.clip_layer import path_for
    from mcrawsome.recipe.load import RecipeError, load_toml, lower_layer_paths

    rotation = 0
    for path in [*lower_layer_paths(clip), path_for(clip, first_16mib)]:
        if path.is_file():
            develop = load_toml(path).get("develop", {})
            if isinstance(develop, dict) and "rotation" in develop:
                rotation = develop["rotation"]
    try:
        return RawCalibration(rotation=rotation).rotation
    except ValueError as exc:
        raise RecipeError(f"develop.rotation: {exc}", field="develop.rotation") from None
