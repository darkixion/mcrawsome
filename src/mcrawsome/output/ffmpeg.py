"""Finding FFmpeg, which decodes and probes MotionCam's LOG recordings (it runs as a separate program)."""

from __future__ import annotations

import os
import shutil
from fractions import Fraction
from pathlib import Path

_STANDARD = [
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(48),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
]


class FfmpegError(Exception):
    """FFmpeg is missing."""


def find_ffmpeg() -> tuple[Path, Path]:
    """The MCRAWSOME_FFMPEG override and the ffprobe beside it, or both from PATH."""
    override = os.environ.get("MCRAWSOME_FFMPEG")  # an empty value counts as unset
    if override:
        ffmpeg = Path(override).expanduser().absolute()
        ffprobe = ffmpeg.with_name("ffprobe")
        if any(not p.is_file() or not os.access(p, os.X_OK) for p in (ffmpeg, ffprobe)):
            raise FfmpegError(
                f"MCRAWSOME_FFMPEG needs an executable pair: {ffmpeg} and {ffprobe}; "
                "check both paths and permissions"
            )
        return ffmpeg, ffprobe
    found_ffmpeg, found_ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not found_ffmpeg or not found_ffprobe:
        raise FfmpegError(
            "FFmpeg (ffmpeg and ffprobe) was not found on PATH; install it to open LOG recordings"
        )
    return Path(found_ffmpeg), Path(found_ffprobe)


def ffprobe_for(ffmpeg: Path) -> Path:
    """ffprobe from PATH for FFmpeg from PATH, otherwise the one beside `ffmpeg`."""
    if (
        not os.environ.get("MCRAWSOME_FFMPEG")
        and (found := shutil.which("ffmpeg"))
        and Path(found) == ffmpeg
        and (probe := shutil.which("ffprobe"))
    ):
        return Path(probe)
    return ffmpeg.with_name("ffprobe")


def frame_rate(fps: float) -> Fraction:
    """The nearest standard rate within 0.2%, otherwise the measured rate."""
    nearest = min(_STANDARD, key=lambda rate: abs(fps - float(rate)))
    if abs(fps - float(nearest)) / float(nearest) < 0.002:
        return nearest
    return Fraction(fps).limit_denominator(1000)
