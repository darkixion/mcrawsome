"""MotionCam Direct Log videos: ordinary video files recorded with a log (or other) colour encoding,
with the gyro embedded as GoPro-format telemetry.

MotionCam can write the same generic container colour tags for different recording profiles.
Their profile is inferred from MotionCam's file-name tokens, or set explicitly with
`develop.input_gamut` and `develop.input_transfer` (or legacy `develop.input_encoding`). Supported
standard metadata is a fallback for other videos. Inference is labelled separately from embedded tags.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

from mcrawsome.colour.encoding import LOG_GAMUTS, ColourEncoding
from mcrawsome.colour.names import colour_from_name
from mcrawsome.io.clip import AudioTrack
from mcrawsome.io.gpmf import GpmfError, extract_gpmf, gyro_log_from_gpmf, read_gpmf
from mcrawsome.io.gyro import GyroLog
from mcrawsome.io.video_timing import VideoTimingError, presentation_times
from mcrawsome.output.ffmpeg import find_ffmpeg

ENCODING_NAMES: dict[str, str] = {
    "davinci": "DaVinci Wide Gamut / DaVinci Intermediate",
    "bt709": "BT.709 primaries / BT.709 transfer (not a log curve: highlights clip early)",
    "acescct": "ACES AP1 / ACEScct",
}
_NAME_PATTERNS: list[tuple[str, str]] = [
    (r"_davinci_davinci-wg(\.|$)", "davinci"),
    (r"_bt\.?709_bt\.?709(\.|$)", "bt709"),
]
VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv")
UNKNOWN_COLOUR = (
    "the recording colour profile is not identified by its file name or supported metadata. Choose the input "
    "colour space and transfer function in Resolve Settings, or set --develop.input_gamut and "
    "--develop.input_transfer "
    "(registry ids, such as s-gamut3-cine and s-log3), or --develop.input_encoding "
    "(davinci, bt709 or acescct)"
)


class DirectLogError(Exception):
    """The video cannot be used."""


def encoding_from_name(name: str) -> str | None:
    """The key (`mcrawsome.colour.encoding`) of the colour encoding a file name carries: MotionCam's
    tokens, then the legacy patterns, which ignore case."""
    pair = colour_from_name(name)
    if pair is not None:
        return ColourEncoding(*pair).key
    lowered = name.lower()
    for pattern, encoding in _NAME_PATTERNS:
        if re.search(pattern, lowered):
            return encoding
    return None


def is_video_input(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


@dataclass(frozen=True)
class DirectLogInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    frame_count: int
    duration_s: float
    codec: str
    pix_fmt: str
    colour_range: str
    encoding: str | None  # the inferred MotionCam pair, or a supported embedded encoding
    audio_rate: int | None
    audio_channels: int | None
    has_telemetry: bool
    tags: dict[str, str] = field(default_factory=dict)
    rotation: int = 0  # clockwise display turn; width and height remain the recorded size
    created: datetime | None = None  # the container's creation time, when it records one
    variable_frame_rate: bool = False
    encoding_source: str = "Not identified"
    colour_note: str | None = None
    log_profile: str | None = None  # embedded evidence only, never inferred from the file name

    @property
    def display_size(self) -> tuple[int, int]:
        return (self.height, self.width) if self.rotation % 180 else (self.width, self.height)

    @property
    def bit_depth(self) -> int:
        match = re.search(r"p(\d+)", self.pix_fmt)
        return int(match.group(1)) if match else 8


def _rate(text: str | None) -> Fraction:
    try:
        return Fraction(text or "0/1")
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _created(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


class NotLogVideoError(DirectLogError):
    """Readable video whose embedded metadata does not establish a Log recording."""


def _token(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


# Recognised recording curves. SDR, scene-linear, PQ and HLG are deliberately absent.
_LOG_ALIASES = {
    "davinci": "davinci-intermediate",
    "davinciintermediate": "davinci-intermediate",
    "acescct": "acescct",
    "cineon": "cineon",
    "vlog": "v-log",
    "panasonicvlog": "v-log",
    "logc3": "logc3",
    "arrilogc3": "logc3",
    "logc4": "LogC4",
    "arrilogc4": "LogC4",
    "slog2": "S-Log2",
    "sonyslog2": "S-Log2",
    "slog3": "s-log3",
    "sonyslog3": "s-log3",
    "canonlog": "Canon Log",
    "clog": "Canon Log",
    "canonlog2": "Canon Log 2",
    "clog2": "Canon Log 2",
    "canonlog3": "canon-log3",
    "clog3": "canon-log3",
    "applelog": "apple-log",
    "applelog2": "Apple Log 2",
    "samsunglog": "samsung-log",
    "flog": "f-log",
    "fujifilmflog": "f-log",
    "flog2": "f-log2",
    "fujifilmflog2": "f-log2",
    "log": "Log (100:1)",
    "log100": "Log (100:1)",
    "logsqrt": "Log (316:1)",
    "log316": "Log (316:1)",
    "dlog": "DJI D-Log",
    "djidlog": "DJI D-Log",
    "dlogm": "DJI D-Log M",
    "nlog": "Nikon N-Log",
    "nikonnlog": "Nikon N-Log",
}
_PROFILE_KEYS = {
    "colortransfer",
    "colortrc",
    "transfercharacteristics",
    "transferfunction",
    "gammacurve",
    "gamma",
    "recordinggamma",
    "capturegamma",
    "logtransferfunction",
    "logprofile",
    "colorprofile",
    "colourprofile",
    "pictureprofile",
    "recordingprofile",
    "comapplequicktimecolortransferfunction",
    "comapplequicktimelogtransferfunction",
}


def embedded_log_profile(video: Mapping[str, object], container: Mapping[str, object]) -> str | None:
    """Accept exact Log declarations in video/format metadata, not codec profiles or free text.

    Primaries describe gamut and cannot establish a recording transfer. A missing declaration stays
    unknown, even for 10/12-bit video, telemetry, or a file named after a Log profile.
    """
    declarations: set[str] = set()
    explicit_nonlog = False
    for group in (video, video.get("tags", {}), container.get("tags", {})):
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if _token(key) not in _PROFILE_KEYS:
                continue
            token = _token(value)
            profile = _LOG_ALIASES.get(token)
            if profile is not None:
                declarations.add(profile)
            elif token in {
                "bt709",
                "rec709",
                "srgb",
                "iec6196621",
                "pq",
                "smpte2084",
                "hlg",
                "aribstdb67",
                "linear",
            }:
                # Generic stream tags are sometimes wrong for camera Log. A dedicated camera profile
                # can identify that case; conflicting dedicated declarations cannot.
                explicit_nonlog |= _token(key) not in {"colortransfer", "colortrc"}
    return next(iter(declarations)) if len(declarations) == 1 and not explicit_nonlog else None


def require_log_video(info: DirectLogInfo) -> None:
    if info.log_profile is None:
        raise NotLogVideoError(
            f"{info.path.name}: not imported because embedded metadata does not identify a Log transfer "
            "function. Ordinary SDR, PQ/HLG, linear and unidentified videos are not Log recordings."
        )


def probe_direct_log(ffprobe: Path, path: Path) -> DirectLogInfo:
    run = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        raise DirectLogError(f"{path.name}: not a readable video ({run.stderr.strip()[-200:]})")
    data = json.loads(run.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise DirectLogError(f"{path.name}: has no video stream")
    codec = video.get("codec_name", "unknown")
    nominal = _rate(video.get("r_frame_rate")) or _rate(video.get("avg_frame_rate"))
    if not nominal:
        raise DirectLogError(f"{path.name}: the video has no frame rate")
    matrix_rotation = next((d["rotation"] for d in video.get("side_data_list", []) if "rotation" in d), None)
    try:
        # ffprobe reports display matrices anticlockwise; the legacy rotate tag is clockwise.
        rotation = (
            -float(matrix_rotation)
            if matrix_rotation is not None
            else float(video.get("tags", {}).get("rotate", 0) or 0)
        ) % 360
    except (TypeError, ValueError):
        raise DirectLogError(f"{path.name}: only quarter turns are supported") from None
    if rotation not in (0, 90, 180, 270):
        raise DirectLogError(f"{path.name}: only quarter turns are supported ({rotation:g} degrees)")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    recorded_count = str(video.get("nb_frames") or video.get("nb_read_packets") or "0")
    frames = int(recorded_count) if recorded_count.isdecimal() else 0
    if not frames:
        # MP4/MOV normally provide nb_frames from their sample tables. Containers
        # without a count need a packet-only fallback, never a video decode.
        count_run = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_packets",
                "-show_entries",
                "stream=nb_read_packets",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if count_run.returncode == 0:
            counted = json.loads(count_run.stdout).get("streams", [{}])[0]
            count_text = str(counted.get("nb_read_packets") or "0")
            frames = int(count_text) if count_text.isdecimal() else 0
    variable = False
    if path.is_file():
        try:
            times = presentation_times(ffprobe, path)
        except (VideoTimingError, OSError) as exc:
            raise DirectLogError(f"{path.name}: cannot read frame timing ({exc})") from None
        frames = len(times)
        if frames > 1:
            steps = np.diff(times)
            # Coarse container clocks (for example MKV milliseconds) alternate
            # adjacent tick lengths even for an exactly constant frame rate.
            quantum = float(_rate(video.get("time_base"))) * 1e9
            tolerance = max(quantum, float(np.median(steps)) * 0.002) + 1
            variable = bool(np.max(steps) - np.min(steps) > tolerance)
            median_rate = 1e9 / float(np.median(steps))
            if abs(1e9 / float(nominal) - float(np.median(steps))) > tolerance:
                # r_frame_rate can be a common clock denominator rather than
                # a useful playback rate on VFR inputs.
                nominal = Fraction(median_rate).limit_denominator(1001)
    log_profile = embedded_log_profile(video, data.get("format", {}))
    named = encoding_from_name(path.name)
    has_telemetry = any(s.get("codec_tag_string") == "gpmd" for s in data["streams"])
    colour_tags = {
        "primaries": video.get("color_primaries", "unknown"),
        "transfer": video.get("color_transfer", "unknown"),
        "matrix": video.get("color_space", "unknown"),
    }
    # MotionCam's encoder tags can declare sRGB for both LOG and scene-linear recordings.
    # The owner-approved Auto mode uses MotionCam's file-name tokens and labels that inference.
    # Without tokens, telemetry identifies the producer but supplies no recording colour profile.
    # Do not silently substitute its generic tags when the name no longer identifies the profile.
    declared_motioncam = "motioncam" in str(data["format"].get("tags", {})).lower()
    uncertain = has_telemetry or declared_motioncam
    if has_telemetry and named is None:
        try:
            payload = extract_gpmf(find_ffmpeg()[0], path)
            device = read_gpmf(payload).device if payload is not None else {}
            producer = device.get("DVNM", "").strip().lower()
            uncertain = declared_motioncam or not producer or "motioncam" in producer
        except (GpmfError, OSError, ValueError):
            uncertain = True
    embedded = None if uncertain else encoding_from_tags(colour_tags)
    profile_encoding = (
        ColourEncoding(LOG_GAMUTS[log_profile], log_profile).key if log_profile in LOG_GAMUTS else None
    )
    identified = profile_encoding if log_profile is not None else named or embedded
    return DirectLogInfo(
        path=path,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=nominal,
        frame_count=frames,
        duration_s=float(data["format"].get("duration", 0.0)),
        codec=codec,
        pix_fmt=video.get("pix_fmt", ""),
        colour_range=video.get("color_range", "unknown"),
        encoding=identified,
        audio_rate=int(audio["sample_rate"]) if audio else None,
        audio_channels=int(audio["channels"]) if audio else None,
        has_telemetry=has_telemetry,
        rotation=int(rotation),
        created=_created(data["format"].get("tags", {}).get("creation_time")),
        variable_frame_rate=variable,
        tags=colour_tags,
        encoding_source="Recording profile metadata"
        if profile_encoding is not None
        else "File name (inferred)"
        if named is not None
        else "Container tags"
        if embedded is not None
        else "Not identified",
        log_profile=log_profile,
        colour_note=(
            f"Embedded {log_profile} Log has no supported automatic input transform. Choose the input colour "
            "space and transfer function in Resolve Settings."
        )
        if log_profile is not None and profile_encoding is None
        else (
            "The recording colour profile could not be identified from its file name or supported metadata. "
            "Choose the input colour space and transfer function in Resolve Settings."
        )
        if identified is None
        else None,
    )


def encoding_from_tags(tags: dict[str, str]) -> str | None:
    """Only supported standard primaries/transfers. The YCbCr matrix is independent."""
    gamut = {"bt709": "rec709", "bt2020": "rec2020", "smpte432": "display-p3"}.get(tags.get("primaries", ""))
    curve = {
        "bt709": "bt709",
        "iec61966-2-1": "srgb",
        "smpte2084": "pq",
        "arib-std-b67": "hlg",
        "linear": "linear",
    }.get(tags.get("transfer", ""))
    return ColourEncoding(gamut, curve).key if gamut is not None and curve is not None else None


def read_audio(ffmpeg: Path, info: DirectLogInfo) -> AudioTrack | None:
    """The file's audio as 16-bit samples; it starts with the first frame (offset 0)."""
    if not info.audio_rate or not info.audio_channels:
        return None
    run = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-i",
            str(info.path),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ],
        capture_output=True,
    )
    if run.returncode != 0:
        raise DirectLogError(f"{info.path.name}: cannot read the audio")
    samples = np.frombuffer(run.stdout, "<i2").reshape(-1, info.audio_channels).astype(np.int16)
    return AudioTrack(samples=samples, sample_rate=info.audio_rate, first_timestamp_ns=0)


def read_gyro(ffmpeg: Path, info: DirectLogInfo) -> tuple[GyroLog | None, dict[str, str]]:
    """The embedded gyro log and the telemetry's device information (camera, EIS flags)."""
    if not info.has_telemetry:
        return None, {}
    try:
        data = extract_gpmf(ffmpeg, info.path)
        if data is None:
            return None, {}
        streams = read_gpmf(data)
        return gyro_log_from_gpmf(streams), streams.device
    except GpmfError:
        return None, {}


def camera_from_minf(minf: str) -> tuple[str | None, float | None]:
    """'pixel 10 pro xl 25.37mm f/1.7' -> ('pixel 10 pro xl', 25.37)."""
    match = re.match(r"(.*?)\s+([\d.]+)mm", minf)
    if not match:
        return (minf or None), None
    return match.group(1), float(match.group(2))


def aperture_from_minf(minf: str) -> float | None:
    match = re.search(r"f/([\d.]+)", minf)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def telemetry_summary(log: GyroLog | None) -> dict[str, object]:
    """Small, useful recorded facts; sensor samples remain in the gyro/analytics views."""
    if log is None or not len(log.times_s):
        return {"gyro_samples": 0, "accelerometer": False}
    return {
        "gyro_samples": len(log.times_s),
        "accelerometer": log.accel is not None,
        "start_s": float(log.times_s[0]),
        "end_s": float(log.times_s[-1]),
        "axes": log.orientation,
        "clock": log.clock,
    }
