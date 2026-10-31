"""`mcrawsome doctor`: what works on this machine and what does not, with the fix for each problem."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mcrawsome import __version__
from mcrawsome.camera.profile import ProfileError, load_profile
from mcrawsome.camera.profile import builtin_dir as camera_builtin
from mcrawsome.camera.profile import user_dir as camera_user
from mcrawsome.io import vaapi
from mcrawsome.output.ffmpeg import FfmpegError, find_ffmpeg
from mcrawsome.paths import config_dir, state_dir
from mcrawsome.pipeline.render import available_memory
from mcrawsome.pipeline.thermal import find_cpu_sensor, read_celsius
from mcrawsome.recipe.load import RecipeError, build_recipe, load_toml
from mcrawsome.runtime import default_workers as default_workers

GB = 1000**3
LOW_SPACE, NO_SPACE = 10 * GB, 1 * GB
LOW_MEMORY = 4 * GB


class Check(BaseModel):
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str
    fix: str | None = None


def _ok(name: str, detail: str) -> Check:
    return Check(name=name, status="ok", detail=detail)


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


_CODEC_LABELS = {"hevc": "H.265", "av1": "AV1"}
# MotionCam's LOG recordings, by FFmpeg's decoder names: (label, decoders that will do).
_LOG_DECODERS = (
    ("H.265 decoder", ("hevc",)),
    ("ProRes decoder", ("prores",)),
    ("AV1 decoder", ("libdav1d", "libaom-av1", "av1")),
)


def _ffmpeg_checks(refresh: bool) -> list[Check]:
    """FFmpeg reads MotionCam's LOG recordings (H.265, ProRes-compatible and AV1); RAW does not need it."""
    install = "Install FFmpeg (package 'ffmpeg'); it is needed only for LOG recordings, not RAW"
    try:
        ffmpeg_path, ffprobe_path = find_ffmpeg()
    except FfmpegError as exc:
        if os.environ.get("MCRAWSOME_FFMPEG"):
            install = (
                "Correct MCRAWSOME_FFMPEG so it names an executable ffmpeg with ffprobe beside it, "
                "or unset it"
            )
        return [
            Check(name=name, status="warn", detail=str(exc), fix=install) for name in ("FFmpeg", "ffprobe")
        ]
    checks: list[Check] = []
    for name, path in (("FFmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        first = _run([str(path), "-version"]).partition("\n")[0].partition(" Copyright")[0].strip()
        checks.append(_ok(name, f"{first or 'version unknown'} ({path})"))
    decoders = {line.split()[1] for line in _run([str(ffmpeg_path), "-hide_banner", "-decoders"]).splitlines()
                if len(line.split()) > 1 and line.split()[0].startswith("V")}  # fmt: skip
    for label, names in _LOG_DECODERS:
        found = next((name for name in names if name in decoders), None)
        checks.append(
            _ok(label, found) if found else
            Check(name=label, status="warn", detail="your FFmpeg has none",
                  fix=f"{install}; LOG recordings in this format need it")
        )  # fmt: skip
    return checks + _decoders(Path(ffmpeg_path), refresh)


def _decoders(ffmpeg: Path, refresh: bool) -> list[Check]:
    """GPU decoding of Direct Log videos (VAAPI, or NVDEC on NVIDIA), from the trial decodes
    (mcrawsome.io.vaapi)."""
    checks = []
    used = os.environ.get("MCRAWSOME_VAAPI_DECODE") == "1"
    for probe in vaapi.probe_decoders(ffmpeg, refresh=refresh):
        label, name = _CODEC_LABELS[probe.codec], f"decoder gpu {probe.codec}"
        device = vaapi.label(probe.device) if probe.device else ""
        if probe.works and used:
            checks.append(_ok(name, f"{label} Direct Log videos decode on {device}"))
        elif probe.works:
            detail = (
                f"{label}: decodes like FFmpeg's own decoder on {device}; not used, as it was no "
                "faster here (MCRAWSOME_VAAPI_DECODE=1 uses it)"
            )
            checks.append(_ok(name, detail))
        else:
            fix = "Only makes decoding faster: videos decode on the processor; "
            checks.append(
                Check(
                    name=name,
                    status="warn",
                    detail=f"{label}: {probe.reason}",
                    fix=fix + "'mcrawsome doctor --refresh' tries again",
                )
            )
    return checks


def _native() -> Check:
    try:
        import mcrawsome._raw  # noqa: F401
    except ImportError as exc:
        return Check(
            name="native extension",
            status="fail",
            detail=f"cannot load mcrawsome._raw ({exc})",
            fix="Reinstall MCRAWsome: 'uv sync --reinstall-package mcrawsome'",
        )
    return _ok("native extension", "loaded")


def _preview_compute() -> Check:
    from mcrawsome.preview.backend import PreviewBackend, gpu_enabled
    from mcrawsome.server.settings import load_settings

    selected = PreviewBackend(enabled=load_settings()[0].preview_gpu and gpu_enabled())
    try:
        device = selected._device()
        if device is not None:
            return _ok(
                "preview compute", f"{selected.name}; driver {device.driver}; available for fast previews"
            )
        detail = f"CPU ({selected.reason})"
        if "disabled" in selected.reason:
            return _ok("preview compute", detail)
        return Check(
            name="preview compute",
            status="warn",
            detail=detail,
            fix="Optional: install the host GPU's OpenCL ICD and give the user access to its render device",
        )
    finally:
        selected.close()


def _profiles() -> Check:
    bad: list[str] = []
    count = 0
    for folder in (camera_user(), camera_builtin()):
        for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
            count += 1
            try:
                load_profile(path)
            except ProfileError as exc:
                bad.append(str(exc))
    if bad:
        return Check(
            name="camera profiles",
            status="fail",
            detail="; ".join(bad),
            fix=f"Correct or delete the profile file(s) in {camera_user()}",
        )
    return _ok("camera profiles", f"{count} parse")


def _defaults() -> Check:
    path = config_dir() / "defaults.toml"
    if not path.exists():
        return _ok("personal defaults", "none set")
    try:
        build_recipe([load_toml(path)])
    except RecipeError as exc:
        return Check(
            name="personal defaults", status="fail", detail=str(exc), fix=f"Correct or delete {path}"
        )
    return _ok("personal defaults", f"{path} parses")


def _memory() -> Check:
    available = available_memory()
    workers = default_workers()
    if available is None:
        return _ok("memory", f"unknown; the default is {workers} workers")
    detail = f"{available / GB:.1f} GB available; the default is {workers} workers"
    if available < LOW_MEMORY:
        return Check(
            name="memory",
            status="warn",
            detail=detail,
            fix="Close other programs, or use --workers 1 or 2 for noise reduction and step printing",
        )
    return _ok("memory", detail)


def _cpu_temperature() -> Check:
    sensor = find_cpu_sensor()
    if sensor is None:
        return Check(
            name="CPU temperature",
            status="warn",
            detail="no sensor found",
            fix="--thermal-limit has no effect here; watch the temperature yourself on long renders",
        )
    value = read_celsius(sensor)
    if value is None:
        return Check(
            name="CPU temperature",
            status="warn",
            detail=f"{sensor} cannot be read",
            fix="--thermal-limit has no effect until the sensor can be read",
        )
    return _ok("CPU temperature", f"{value:.0f} C ({sensor})")


def _space(name: str, folder: Path) -> Check:
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    free = shutil.disk_usage(folder).free
    detail = f"{free / GB:.1f} GB free in {folder}"
    fix = "Free some space, or export to another disk with --output"
    if free < NO_SPACE:
        return Check(name=name, status="fail", detail=detail, fix=fix)
    if free < LOW_SPACE:
        return Check(name=name, status="warn", detail=detail, fix=fix)
    return _ok(name, detail)


def _job_store() -> Check:
    folder = state_dir() / "jobs"
    parent = folder
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.is_dir() and os.access(parent, os.W_OK | os.X_OK):
        return _ok("job store", str(folder))
    return Check(
        name="job store",
        status="fail",
        detail=f"{folder} cannot be created or written",
        fix="Make the folder writable, or set MCRAWSOME_STATE_DIR to one that is",
    )


def run_checks(refresh: bool = False) -> list[Check]:
    """Every check, in the order doctor prints them. `refresh` tries the GPU decoders again."""
    info: list[Callable[[], Check]] = [
        lambda: _ok("Python", sys.version.split()[0]),
        lambda: _ok("MCRAWsome", __version__),
        _native,
        _preview_compute,
    ]
    after: list[Callable[[], Check]] = [
        _profiles,
        _defaults,
        _memory,
        lambda: _ok("CPU", f"{os.cpu_count() or 1} cores"),
        _cpu_temperature,
        lambda: _space("free space here", Path.cwd()),
        lambda: _space("free space in the state folder", state_dir()),
        _job_store,
    ]
    return [
        *(check() for check in info),
        *_ffmpeg_checks(refresh),
        *(check() for check in after),
    ]
