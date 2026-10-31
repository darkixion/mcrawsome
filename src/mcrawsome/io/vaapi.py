"""Decoding H.265 and AV1 videos on the GPU's video engine: through VAAPI (Intel and AMD GPUs), or else
NVDEC (NVIDIA GPUs, as devices `cuda:N`).

Whether it works is found by decoding a short 10-bit clip of each codec (in data/) on each render device,
then each NVIDIA GPU, and comparing the frames with FFmpeg's own decoder, cached until FFmpeg or the
devices change, like the encoder trials. A video then decodes with `-hwaccel vaapi` or `-hwaccel cuda`,
frames downloaded to 10 bits; for a stream the device cannot decode (a profile it lacks), FFmpeg goes back
to its own decoder by itself.

Export decoding requires MCRAWSOME_VAAPI_DECODE=1 (for either kind of GPU). Persistent previews opt in
automatically unless MCRAWSOME_VAAPI_DECODE=0 explicitly disables it. Earlier single-frame measurements
included device setup for each seek; persistent preview lanes retain that setup and, on VAAPI, scale
before downloading.

"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ValidationError

from mcrawsome.paths import cache_dir

TRIAL_TIMEOUT_S = 20
_NOISE = (
    "Task finished",
    "Terminating thread",
    "Error sending frames",
    "Nothing was written",
    "Error parsing global options",
    "Error opening output file",
    "Error while filtering",
    "Conversion failed",
)


def _reason(stderr: str) -> str:
    """FFmpeg's first few distinct messages, without the [component @ address] prefixes."""
    lines: list[str] = []
    for raw in stderr.splitlines():
        line = re.sub(r"^(\[[^\]]*\]\s*)+", "", raw).strip()
        if line and not line.startswith(_NOISE) and line not in lines:
            lines.append(line)
    return "; ".join(lines[:3])[:400] or "the trial decode failed"


def _vaapi_devices() -> list[str]:
    override = os.environ.get("MCRAWSOME_VAAPI_DEVICE")
    return [override] if override else sorted(glob.glob("/dev/dri/renderD*"))


def _nvidia_devices() -> list[str]:
    """One CUDA device per NVIDIA GPU node (/dev/nvidia0 and on; the driver's control nodes aside)."""
    count = sum(bool(re.fullmatch(r"/dev/nvidia\d+", node)) for node in glob.glob("/dev/nvidia*"))
    return [f"cuda:{index}" for index in range(count)]


def hwaccel(device: str) -> list[str]:
    """FFmpeg's options to decode on `device`: a VAAPI render node, or an NVIDIA GPU as `cuda:N`."""
    if device.startswith("cuda:"):
        return ["-hwaccel", "cuda", "-hwaccel_device", device.removeprefix("cuda:")]
    return ["-hwaccel", "vaapi", "-hwaccel_device", device]


def label(device: str) -> str:
    return f"NVIDIA GPU {device.removeprefix('cuda:')}" if device.startswith("cuda:") else device


def _output(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=TRIAL_TIMEOUT_S).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _cache_key(ffmpeg: Path) -> dict[str, Any]:
    version = _output([str(ffmpeg), "-version"]).partition("\n")[0].strip()
    devices = sorted(glob.glob("/dev/dri/renderD*") + glob.glob("/dev/nvidia*"))
    return {
        "ffmpeg": str(ffmpeg),
        "version": version,
        "devices": devices,
        "vaapi_device": os.environ.get("MCRAWSOME_VAAPI_DEVICE"),
    }


CODECS = ("hevc", "av1")  # FFmpeg's codec names
TOLERANCE = 64  # in 16-bit RGB: one 10-bit code
_SCALE = "scale=in_color_matrix=bt709:out_range=full:flags=accurate_rnd+full_chroma_int,format=rgb48le"


class DecoderProbe(BaseModel):
    """Whether a GPU decodes a codec here like FFmpeg's own decoder, and if not, why not."""

    codec: str
    works: bool
    reason: str | None = None
    device: str | None = None  # the render node, or the NVIDIA GPU (cuda:N), that worked


def _sample(codec: str) -> Path:
    return Path(__file__).parent / "data" / f"trial-{codec}.mp4"


def _decode(ffmpeg: Path, sample: Path, device: str | None) -> bytes | str:
    """`sample` as 16-bit RGB, decoded on `device` (or by FFmpeg itself), else why it failed. On a
    device the frames stay on the GPU until downloaded, so a decode FFmpeg quietly did itself fails."""
    surfaces = "cuda" if device and device.startswith("cuda:") else "vaapi"
    hardware = [*hwaccel(device), "-hwaccel_output_format", surfaces] if device else []
    decoder = ["-c:v", "av1"] if device and sample.stem == "trial-av1" else []
    command = [
        str(ffmpeg), "-nostdin", "-hide_banner", "-v", "error", *hardware, *decoder,
        "-i", str(sample), "-vf", f"hwdownload,format=p010le,{_SCALE}" if device else _SCALE,
        "-f", "rawvideo", "-",
    ]  # fmt: skip
    try:
        run = subprocess.run(command, capture_output=True, timeout=TRIAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"the trial decode took longer than {TRIAL_TIMEOUT_S} s"
    except OSError as exc:
        return f"FFmpeg could not be run ({exc})"
    if run.returncode != 0 or not run.stdout:
        return _reason(run.stderr.decode(errors="replace"))
    return run.stdout


def _trial(ffmpeg: Path, codec: str, device: str) -> str | None:
    """None when `device` decodes the codec's trial clip like FFmpeg's own decoder, else why not."""
    software = _decode(ffmpeg, _sample(codec), None)
    if isinstance(software, str):
        return f"FFmpeg's own decoder failed: {software}"
    hardware = _decode(ffmpeg, _sample(codec), device)
    if isinstance(hardware, str):
        return hardware
    if len(hardware) != len(software):
        return "the GPU gave a different number of frames from FFmpeg's own decoder"
    difference = int(
        np.abs(np.frombuffer(hardware, "<u2").astype(np.int32) - np.frombuffer(software, "<u2")).max()
    )
    if difference > TOLERANCE:
        return f"the GPU's frames differ from FFmpeg's own decoder by up to {difference} codes"
    return None


def _probe(ffmpeg: Path, codec: str) -> DecoderProbe:
    devices = [*_vaapi_devices(), *_nvidia_devices()]
    if not devices:
        missing = "no GPU video decoder (/dev/dri/renderD* or /dev/nvidia*) found"
        return DecoderProbe(codec=codec, works=False, reason=missing)
    reasons = []
    for device in devices:
        reason = _trial(ffmpeg, codec, device)
        if reason is None:
            return DecoderProbe(codec=codec, works=True, device=device)
        reasons.append(f"{label(device)}: {reason}" if len(devices) > 1 else reason)
    return DecoderProbe(codec=codec, works=False, reason="; ".join(reasons))


class _Cached(BaseModel):
    key: dict[str, object]
    probes: list[DecoderProbe]


class _CacheFile(BaseModel):
    entries: list[_Cached] = []


def _cache_path() -> Path:
    return cache_dir() / "decoders.json"


def _read_cache() -> _CacheFile:
    try:
        return _CacheFile.model_validate_json(_cache_path().read_bytes())
    except (OSError, ValidationError):
        return _CacheFile()


def _write_cache(cache: _CacheFile) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        part.write_text(json.dumps(cache.model_dump(mode="json"), indent=1), encoding="utf-8")
        part.replace(path)
    except OSError:
        pass  # a cache that cannot be written only means trying again next time


_REMEMBERED: dict[str, list[DecoderProbe]] = {}  # this process's trials, whatever the cache file does


def probe_decoders(ffmpeg: Path, refresh: bool = False) -> list[DecoderProbe]:
    """GPU decoding of each codec in CODECS, tried on each render device, then each NVIDIA GPU. Cached per
    FFmpeg and GPU devices; `refresh` tries again."""
    key = {**_cache_key(ffmpeg), "native_av1": 1, "nvdec": 1}
    remembered = json.dumps(key, sort_keys=True)
    if not refresh and remembered in _REMEMBERED:
        return _REMEMBERED[remembered]
    cache = _read_cache()
    found = None if refresh else next((entry.probes for entry in cache.entries if entry.key == key), None)
    if found is None:
        found = [_probe(ffmpeg, codec) for codec in CODECS]
        entries = [entry for entry in cache.entries if entry.key.get("ffmpeg") != key["ffmpeg"]]
        _write_cache(_CacheFile(entries=[*entries, _Cached(key=key, probes=found)]))
    _REMEMBERED[remembered] = found
    return found


def decode_device(ffmpeg: Path, codec: str | None, *, preview: bool = False) -> str | None:
    """The device (a render node, or an NVIDIA GPU as cuda:N) that decodes a video in `codec` (FFmpeg's
    name), or None to decode it on the processor for other codecs or a failed trial. Previews opt in by
    default; exports require the environment switch. An explicit 0 disables both."""
    enabled = os.environ.get("MCRAWSOME_VAAPI_DECODE")
    if codec not in CODECS or (enabled != "1" and not (preview and enabled is None)):
        return None
    probe = next(p for p in probe_decoders(ffmpeg) if p.codec == codec)
    return probe.device if probe.works else None
