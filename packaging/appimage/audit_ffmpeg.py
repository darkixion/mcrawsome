"""Check the AppImage's private FFmpeg: it decodes what MCRAWsome reads, is LGPL, and loads no FFmpeg
library from the host.

Usage: audit_ffmpeg.py FFMPEG FFPROBE. Prints what was checked as JSON; exits non-zero on a gap.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

# Direct Log's video and audio, as MCRAWsome reads them (io.directlog, preview.video, io.vaapi, motion).
DECODERS = ("hevc", "prores", "h264", "libdav1d", "av1", "aac", "pcm_s16le", "pcm_s24le")
DEMUXERS = ("mov", "matroska")
MUXERS = ("rawvideo", "s16le", "data", "wav")
ENCODERS = ("rawvideo", "pcm_s16le")
FILTERS = ("scale", "format", "select", "hwdownload", "null")
SHARED = re.compile(r"lib(avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|dav1d)\.so")


def listed(ffmpeg: str, kind: str) -> set[str]:
    output = subprocess.run([ffmpeg, "-hide_banner", f"-{kind}"], capture_output=True, text=True, check=True)
    names = set()
    for line in output.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and not line.startswith(" ="):
            names.update(fields[1].split(","))
    return names


def main() -> None:
    ffmpeg, ffprobe = sys.argv[1:3]
    problems = []
    found: dict[str, list[str]] = {}
    for kind, wanted in (
        ("decoders", DECODERS),
        ("demuxers", DEMUXERS),
        ("muxers", MUXERS),
        ("encoders", ENCODERS),
        ("filters", FILTERS),
    ):
        names = listed(ffmpeg, kind)
        missing = [name for name in wanted if name not in names]
        problems += [f"missing {kind[:-1]} {name}" for name in missing]
        found[kind] = [name for name in wanted if name in names]
    hardware = subprocess.run([ffmpeg, "-hide_banner", "-hwaccels"], capture_output=True, text=True).stdout
    if "vaapi" not in hardware:
        problems.append("no VAAPI hardware decoding")
    if "cuda" not in hardware:
        problems.append("no NVIDIA (NVDEC) hardware decoding")
    version = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, check=True).stdout
    if "--enable-gpl" in version or "--enable-nonfree" in version:
        problems.append("FFmpeg must be LGPL: neither --enable-gpl nor --enable-nonfree")
    for program in (ffmpeg, ffprobe):
        libraries = subprocess.run(["ldd", program], capture_output=True, text=True).stdout
        if SHARED.search(libraries):
            problems.append(f"{program} loads a shared FFmpeg or dav1d library")
    print(json.dumps({"version": version.splitlines()[0], "found": found, "problems": problems}, indent=2))
    if problems:
        raise SystemExit("; ".join(problems))


if __name__ == "__main__":
    main()
