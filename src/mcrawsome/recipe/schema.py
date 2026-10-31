"""The Recipe: every user-controllable setting, defined once."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcrawsome.camera.vignette import VignetteProfile
from mcrawsome.colour.encoding import (
    LOGC3_DEFAULT_EI,
    GamutId,
    TransferId,
)
from mcrawsome.colour.transfer import LOGC3_EI
from mcrawsome.develop.crop import parse_crop
from mcrawsome.recipe.raw import RawCalibration as RawCalibration
from mcrawsome.recipe.raw import number_list as _number_list
from mcrawsome.recipe.times import parse_time

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class DevelopParams(RawCalibration):
    model_config = _FROZEN

    exposure: Annotated[float, Field(ge=-6.0, le=6.0)] | Literal["auto"] = Field(
        "auto",
        description="Exposure adjustment in stops, or auto (measured from the clip)",
        json_schema_extra={"layer": 2},
    )
    input_encoding: Literal["auto", "davinci", "bt709", "acescct"] = Field(
        "auto",
        description="Video inputs: colour encoding (auto reads MotionCam's file name or supported metadata)",
        json_schema_extra={"layer": 3},
    )
    input_gamut: Literal["auto"] | GamutId = Field(
        "auto",
        description="Video inputs: the gamut, by id (auto reads MotionCam's file name or supported metadata)",
        json_schema_extra={"layer": 3},
    )
    input_transfer: Literal["auto"] | TransferId = Field(
        "auto",
        description=(
            "Video inputs: the transfer function, by id "
            "(auto reads MotionCam's file name or supported metadata)"
        ),
        json_schema_extra={"layer": 3},
    )
    input_ei: int = Field(
        LOGC3_DEFAULT_EI,
        description="Video inputs in ARRI LogC3: the exposure index the clip was encoded at (160 to 1600)",
        json_schema_extra={"layer": 3},
    )
    native_as: Literal["unset", "ap1"] = Field(
        "unset",
        description=(
            "Camera Native video inputs: ap1 reads their colours as ACES AP1 for any output but Camera "
            "Native log; unset refuses them unless camera.colour_matrix gives the phone's matrix"
        ),
        json_schema_extra={"layer": 3},
    )
    highlight_recovery: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Rebuild clipped colour channels from the others (0 off; RAW only)",
        json_schema_extra={"layer": 2},
    )
    vignette_profile: VignetteProfile | None = Field(
        None,
        description="RAW video lens-shading profile replacing the recorded gain maps",
        json_schema_extra={"layer": 3},
    )
    capture_sharpening: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="RAW video only: noise-aware capture sharpening before stabilisation (0 off)",
        json_schema_extra={"layer": 2},
    )
    capture_radius: float = Field(
        0.8,
        ge=0.5,
        le=1.2,
        description="RAW video capture sharpening radius in recorded pixels (0.5 to 1.2)",
        json_schema_extra={"layer": 3},
    )
    deflicker: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "RAW only: even out exposure jumps and flicker between frames (timelapses, misbehaving auto "
            "exposure): 1 fully, 0 off"
        ),
        json_schema_extra={"layer": 2},
    )
    deflicker_seconds: float = Field(
        1.0,
        ge=0.1,
        le=60.0,
        description="Deflicker: the window the exposure is averaged over (longer keeps only slower changes)",
        json_schema_extra={"layer": 3},
    )
    white_balance: Literal["median", "first", "custom"] = Field(
        "median",
        description="White balance: median as-shot, first frame, or custom temperature and tint",
        json_schema_extra={"layer": 3},
    )
    temperature: float | None = Field(
        None,
        ge=2000,
        le=12000,
        description="Custom white balance temperature in Kelvin (2000 to 12000)",
        json_schema_extra={"layer": 3},
    )
    tint: float | None = Field(
        None,
        ge=-100,
        le=100,
        description="Custom white balance tint (-100 to 100)",
        json_schema_extra={"layer": 3},
    )

    @model_validator(mode="after")
    def _custom_white_balance(self) -> DevelopParams:
        if self.white_balance == "custom" and (self.temperature is None or self.tint is None):
            raise ValueError("Custom white balance requires both temperature and tint")
        return self

    crop: str = Field(
        "none",
        description=(
            "RAW only: none; auto removes a black strip on any edge (some 60 fps recordings); or a size "
            "such as 3840x2160, from the top-left corner (for a resolution mismatch), or placed, such as "
            "3840x2160+64+32"
        ),
        json_schema_extra={"layer": 3},
    )

    @field_validator("input_ei")
    @classmethod
    def _input_ei(cls, value: int) -> int:
        return _exposure_index(value)

    @field_validator("crop")
    @classmethod
    def _crop_syntax(cls, value: str) -> str:
        parse_crop(value)
        return value


def _exposure_index(value: int) -> int:
    if value not in LOGC3_EI:
        raise ValueError(f"ARRI LogC3 has no EI {value}; use one of {', '.join(map(str, LOGC3_EI))}")
    return value


# Settings older versions saved, for looks, effects, noise reduction and video encoding, which MCRAWsome
# no longer has: a saved recipe that names them still loads, without them.
REMOVED = frozenset({"look", "hdr", "effects", "denoise", "grade"})
REMOVED_OUTPUT = frozenset(
    {"preset", "crf", "encoder", "hdr_format", "log", "log_gamut", "log_ei", "prores_profile",
     "dnxhr_profile", "apv_quality"}
)  # fmt: skip
VIDEO_KINDS = frozenset({"av1", "hevc", "h264", "vp9", "hdr-sdr", "prores", "dnxhr", "apv"})


def without_removed(data: Any) -> Any:
    """A saved recipe layer without the settings MCRAWsome no longer has."""
    if not isinstance(data, dict):
        return data
    kept = {key: value for key, value in data.items() if key not in REMOVED}
    output = kept.get("output")
    if isinstance(output, dict):
        kept["output"] = {key: value for key, value in output.items() if key not in REMOVED_OUTPUT}
    return kept


class OutputParams(BaseModel):
    model_config = _FROZEN

    kind: Literal["dng", "exr", "mcraw"] = Field(
        "dng",
        description=(
            "What to make: dng (a CinemaDNG folder), exr (an OpenEXR folder in scene linear), or mcraw "
            "(a lossless RAW copy or repair)"
        ),
        json_schema_extra={"layer": 1},
    )
    exr_space: Literal["acescg", "linear-rec709"] = Field(
        "acescg",
        description="OpenEXR only: scene linear in ACEScg (AP1) or Linear Rec.709",
        json_schema_extra={"layer": 2},
    )
    exr_compression: Literal["zip", "piz"] = Field(
        "zip", description="OpenEXR only: lossless ZIP or PIZ compression", json_schema_extra={"layer": 3}
    )
    timecode: Literal["zero", "time-of-day"] = Field(
        "zero",
        description=(
            "CinemaDNG and OpenEXR: zero numbers the frames from 00:00:00:00; time-of-day starts at the time "
            "the first frame was recorded (the phone's clock), in the frames, and in the WAV as a Broadcast "
            "Wave time reference"
        ),
        json_schema_extra={"layer": 3},
    )
    timecode_zone: str | None = Field(
        None,
        description="Time-of-day timecode's IANA time zone, for example Europe/London; None: this computer's",
        json_schema_extra={"layer": 3},
    )

    @field_validator("timecode_zone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        from mcrawsome.io.timecode import valid_zone

        if value is not None and not valid_zone(value):
            raise ValueError(f"unknown time zone {value!r}")
        return value

    dng_scale: Literal["full", "half", "quarter", "eighth"] = Field(
        "full",
        description=(
            "half makes CinemaDNG proxies at half the width and height; quarter and eighth at a quarter "
            "and an eighth"
        ),
        json_schema_extra={"layer": 2},
    )
    dng_shading: Literal["opcode", "baked"] = Field(
        "opcode",
        description=(
            "CinemaDNG only: opcode stores the lens shading as gain maps for the raw developer; baked "
            "applies it to the raw values, for editors that ignore DNG gain maps"
        ),
        json_schema_extra={"layer": 3},
    )
    dng_scale_raw: bool = Field(
        True,
        description=(
            "Baked lens shading: raise the white level so the correction cannot clip highlights (exposure is "
            "raised to match)"
        ),
        json_schema_extra={"layer": 3},
    )
    dng_deflicker: Literal["baseline", "baked"] = Field(
        "baseline",
        description=(
            "CinemaDNG with develop.deflicker: baseline stores each frame's correction as its "
            "BaselineExposure, keeping the raw values; baked scales the raw values by it, as a mount's "
            "picture exposure normalisation does, for editors that ignore BaselineExposure (up to two stops "
            "of headroom; a frame brightened further clips)"
        ),
        json_schema_extra={"layer": 3},
    )
    dng_transfer: Literal["linear", "log"] = Field(
        "linear",
        description=(
            "CinemaDNG only: log stores log-encoded values with a linearisation table, for smaller DNGs "
            "(see output.dng_reduce_bits)"
        ),
        json_schema_extra={"layer": 3},
    )
    dng_reduce_bits: Literal["auto", 2, 4, 6, 8] = Field(
        "auto",
        description=(
            "Log DNGs: bits removed from the stored values, with dithering (auto stores the sensor's bits "
            "less 2, allowing for a baked vignette's overhead)"
        ),
        json_schema_extra={"layer": 3},
    )
    dng_camera_model: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r"^[ -~]+$",
        description=(
            "CinemaDNG only: the camera model written into the DNGs, for better defaults in Resolve's RAW "
            "tab: Panasonic Varicam RAW, Blackmagic Pocket Cinema Camera 4K, or any name"
        ),
        json_schema_extra={"layer": 3},
    )
    dng_unique_model: bool = Field(
        False,
        description=(
            "CinemaDNG only: add the clip's name to the camera model, so Resolve shows its RAW-tab controls "
            "and keeps each clip's RAW settings"
        ),
        json_schema_extra={"layer": 3},
    )
    dng_compression: Literal["none", "lossless"] = Field(
        "none",
        description="CinemaDNG only: lossless JPEG makes DNG folders about a third of the size",
        json_schema_extra={"layer": 3},
    )
    frame_rate: (
        Literal["keep", "integer", "drop-frame", "average", "median"] | Annotated[float, Field(ge=1, le=1000)]
    ) = Field(
        "keep",
        description=(
            "Keep the recorded rate (VFR LOG follows its timestamps at the nominal rate), "
            "or convert to a constant rate by "
            "repeating and dropping frames: integer (30.33 to 30), drop-frame (23.976, 29.97, 59.94), "
            "average, median (for slow motion) or a number; the sound stays at its recorded speed"
        ),
        json_schema_extra={"layer": 2},
    )
    frame_rate_drift_ms: float = Field(
        20.0,
        ge=1.0,
        le=500.0,
        description="Frame-rate conversion: how far the picture may drift from the sound before a frame is "
        "repeated or dropped (never less than half a frame)",
        json_schema_extra={"layer": 3},
    )
    fill_dropped: bool = Field(
        True,
        description="Repeat a frame for each dropped frame so sound stays in sync",
        json_schema_extra={"layer": 3},
    )

    @field_validator("kind", mode="before")
    @classmethod
    def _no_video(cls, value: Any) -> Any:
        if value in VIDEO_KINDS:
            raise ValueError(
                f"MCRAWsome no longer encodes video ({value}); export dng, exr or mcraw, or send to Resolve"
            )
        return value

    @field_validator("dng_reduce_bits", mode="before")
    @classmethod
    def _bits_number(cls, value: Any) -> Any:
        """--output.dng_reduce_bits 4 arrives as text; the choices are numbers."""
        return int(value) if isinstance(value, str) and value.isdigit() else value


# Stabilising turns each frame within the crop's margin; with none (1.0) the plan backs every correction off.
MIN_STABILISE_CROP = 1.05


class ReviewCategories(BaseModel):
    """Review marker categories; all participate when Review markers is enabled."""

    model_config = _FROZEN
    shake: bool = True
    focus: bool = True
    recorded_tilt: bool = True
    remaining_tilt: bool = True
    clipping: bool = True
    low_signal: bool = True
    gaps: bool = True
    blur: bool = True


class MotionParams(BaseModel):
    """Gyro stabilisation."""

    model_config = _FROZEN

    optical_correction: bool = Field(
        True,
        description="Refine gyro motion with verified image tracking across the clip",
        json_schema_extra={"layer": 2},
    )

    horizon_lock: bool = Field(
        False,
        description=(
            "Resolve timelines only: level the horizon using calibrated accelerometer axes; "
            "crop limits still apply"
        ),
        json_schema_extra={"layer": 2},
    )
    markers: bool = Field(
        False,
        description=(
            "Resolve timelines only: add review suggestions for focus changes, recorded/remaining tilt, "
            "shake, motion blur, sampled RAW clipping/low signal and recording gaps"
        ),
        json_schema_extra={"layer": 2},
    )

    review_categories: ReviewCategories = Field(default_factory=ReviewCategories)

    stabilise: Literal["auto", "on", "off"] = Field(
        "auto",
        description="auto: when a gyro log is found and the sync is confident; on: always, or fail",
        json_schema_extra={"layer": 2},
    )
    crop: float = Field(
        1.10,
        ge=MIN_STABILISE_CROP,
        le=1.5,
        description="Crop (zoom) that hides the moving edges; at least 1.05, since at 1.0 no correction fits",
        json_schema_extra={"layer": 2},
    )

    @field_validator("crop", mode="before")
    @classmethod
    def _crop_margin(cls, value: Any) -> Any:
        """Crops from 1.0 were accepted before the minimum; they could not stabilise, so they load at it."""
        legacy = isinstance(value, int | float) and not isinstance(value, bool)
        return MIN_STABILISE_CROP if legacy and 1.0 <= value < MIN_STABILISE_CROP else value

    strength: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description=(
            "How strongly to smooth: 1 uses motion.smoothing_seconds, 0.5 keeps only the short smoothing "
            "that removes fast shake, 0 is off"
        ),
        json_schema_extra={"layer": 2},
    )
    smoothing_seconds: float = Field(
        0.5,
        ge=0.05,
        le=5.0,
        description="How slowly the stabilised camera may move",
        json_schema_extra={"layer": 3},
    )
    mode: Literal["smooth", "rolling_shutter", "tripod", "pan"] = Field(
        "smooth",
        description=(
            "smooth follows the camera's smoothed path; rolling_shutter straightens rolling-shutter "
            "wobble only and leaves the framing as shot; tripod holds one view, as far as the crop allows; "
            "pan holds tilt and roll and lets the pan follow, smoothed"
        ),
        json_schema_extra={"layer": 2},
    )
    pitch: bool = Field(
        True, description="Correct up-and-down tilt as well as pan and roll", json_schema_extra={"layer": 3}
    )
    bands: int = Field(
        36,
        ge=1,
        le=200,
        description="Row bands for rolling-shutter correction",
        json_schema_extra={"layer": 3},
    )
    min_confidence: float = Field(
        0.6,
        ge=0.0,
        le=1.0,
        description="Least share of image motion the gyro must explain",
        json_schema_extra={"layer": 3},
    )
    search_ms: float | None = Field(
        None,
        ge=1.0,
        le=5000.0,
        description="Sync search window either side (default from the camera)",
        json_schema_extra={"layer": 3},
    )
    sync_frames: int = Field(
        480,
        ge=24,
        le=100_000,
        description="Frames from the middle of the clip used for the sync fit",
        json_schema_extra={"layer": 3},
    )


class CameraParams(BaseModel):
    """Overrides for the camera profile."""

    model_config = _FROZEN

    focal_px: float | None = Field(
        None, gt=0, description="Focal length in pixels", json_schema_extra={"layer": 3}
    )
    readout_ms: float | None = Field(
        None, ge=0, le=100, description="Rolling-shutter readout time", json_schema_extra={"layer": 3}
    )
    sync_ms: float | None = Field(
        None, ge=-2000, le=2000, description="Expected gyro sync offset", json_schema_extra={"layer": 3}
    )
    colour_matrix: tuple[float, float, float, float, float, float, float, float, float] | None = Field(
        None,
        description=(
            "RAW only: a colour matrix (XYZ to camera, as DNG ColorMatrix, nine values row by row) replacing "
            "the clip's, in development and DNGs"
        ),
        json_schema_extra={"layer": 3},
    )

    @field_validator("colour_matrix", mode="before")
    @classmethod
    def _matrix_values(cls, value: Any) -> Any:
        return _number_list(value)

    @field_validator("colour_matrix")
    @classmethod
    def _invertible_matrix(cls, value: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if value is not None:
            matrix = np.asarray(value).reshape(3, 3)
            if not np.isfinite(matrix).all() or np.linalg.matrix_rank(matrix) < 3:
                raise ValueError("the colour matrix must contain finite values and be invertible")
        return value


class TrimParams(BaseModel):
    model_config = _FROZEN

    start: str | None = Field(
        None,
        description="Start time: seconds, HH:MM:SS.s or frames (120f)",
        json_schema_extra={"layer": 1},
    )
    duration: str | None = Field(
        None,
        description="Duration: seconds, HH:MM:SS.s or frames (120f)",
        json_schema_extra={"layer": 1},
    )

    @field_validator("start", "duration", mode="before")
    @classmethod
    def _seconds(cls, value: Any) -> Any:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return str(value)  # start = 2 in TOML, or --set trim.start=2, means seconds
        return value

    @field_validator("start", "duration")
    @classmethod
    def _time_syntax(cls, value: str | None) -> str | None:
        if value is not None:
            parse_time(value, 1.0)  # syntax only; the clip's frame rate is used when planning
        return value


class Recipe(BaseModel):
    model_config = _FROZEN

    develop: DevelopParams = DevelopParams()
    motion: MotionParams = MotionParams()
    camera: CameraParams = CameraParams()
    output: OutputParams = OutputParams()
    trim: TrimParams = TrimParams()

    @model_validator(mode="before")
    @classmethod
    def _removed_settings(cls, data: Any) -> Any:
        return without_removed(data)

    @model_validator(mode="after")
    def _consistent(self) -> Recipe:
        if self.develop.crop != "none" and self.motion.stabilise == "on":
            raise ValueError("a cropped frame (develop.crop) is not stabilised yet; remove one of them")
        if self.develop.input_ei != LOGC3_DEFAULT_EI and self.develop.input_transfer not in ("auto", "logc3"):
            raise ValueError(
                "develop.input_ei needs a LogC3 input (input_transfer logc3, or auto for a LogC3 file)"
            )
        develop = self.develop
        if develop.input_encoding != "auto" and (develop.input_gamut, develop.input_transfer) != (
            "auto",
            "auto",
        ):
            raise ValueError(
                "set develop.input_encoding or develop.input_gamut and develop.input_transfer, not both"
            )
        return self
