"""`mcrawsome support-bundle`: one zip of what a bug report needs, without any picture or sound.

The bundle holds doctor's checks, a description of the system, the sidecars
given and the metadata of the clips given. Every text member has the home folder replaced by `~`.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path
from typing import Any

from mcrawsome import __version__
from mcrawsome.doctor import run_checks
from mcrawsome.io.describe import describe_any
from mcrawsome.output.ffmpeg import FfmpegError, find_ffmpeg
from mcrawsome.paths import state_dir

LOG_LIMIT = 1_000_000  # bytes: no text member is larger
_SIDECAR_SUFFIXES = (".toml",)  # sidecars and Recipe files; anything else given is a clip
_CUT = "(earlier lines cut)\n"

README = """MCRAWsome support bundle

This archive is for a bug report. It holds:

  doctor.json        what 'mcrawsome doctor' found on this machine
  system.json        operating system, kernel, processor, memory, Python, the versions of MCRAWsome's
                     dependencies and 'ffmpeg -version'
  sidecars/<name>    the sidecar and Recipe files given
  clips/<name>.json  the metadata of each clip given, as 'mcrawsome info --json' prints it

It holds no picture or sound: no frames, audio or gyro samples from any clip, and no recording location
(a clip's location is left out). Your home folder is written as ~. File names and folder names are
kept, so look through it before sharing.

Members:
{members}
"""


def _private(text: str) -> str:
    """The home folder replaced by ~ where it is a whole path: starting the text or after a character no
    path name holds here (whitespace, quotes, =, :, (, [, ,), and ending at a path separator or such a
    character. Both the home folder as given and as resolved (a symlinked home) are replaced, longest
    first."""
    homes = {str(Path.home()).rstrip("/"), str(Path.home().resolve()).rstrip("/")} - {""}
    if not homes:
        return text
    either = "|".join(re.escape(home) for home in sorted(homes, key=len, reverse=True))
    return re.sub(rf"(?<![^\s\"'=:(\[,])(?:{either})(?![^/\\\"'\s,:;)\]}}])", "~", text)


def _tail(text: str, limit: int = LOG_LIMIT) -> str:
    """The end of `text` within `limit` bytes, starting at a whole line."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    end = data[-(limit - len(_CUT)) :]
    newline = end.find(b"\n")
    if 0 <= newline < len(end) - 1:
        end = end[newline + 1 :]
    return _CUT + end.decode("utf-8", errors="ignore")


def _read_tail(path: Path, limit: int = LOG_LIMIT) -> str:
    """Read bounded log data even when the file has grown beyond its rotation limit."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        data = stream.read(limit)
    text = data.decode("utf-8", errors="replace")
    return _tail(_CUT + text, limit) if size > limit else text


def diagnostic_members() -> dict[str, str]:
    """An on-demand UI report: no selected media and 64 KiB per member.

    Keep the encoded JSON below the desktop's 8 MiB save limit, even with escaped log characters.
    The CLI bundle retains its larger limits and optional metadata/sidecars.
    """
    limit = 64 * 1024
    members = bundle_members([])
    for name in ("server.log", "server.log.1"):
        try:
            members[name] = _read_tail(state_dir() / name, limit)
        except FileNotFoundError:
            continue  # manually started engines may only log to their terminal
    return {name: _tail(_private(text), limit) for name, text in members.items()}


def _run(command: list[str]) -> str:
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cannot be run ({exc})"
    return (run.stdout + run.stderr).strip()


def _os_name() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", platform.platform())
    except OSError:
        return platform.platform()


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.partition(":")[2].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _memory() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return None


def _dependencies() -> dict[str, str]:
    """The installed version of each package MCRAWsome depends on."""
    versions: dict[str, str] = {}
    try:
        required = metadata.requires("mcrawsome") or []
    except metadata.PackageNotFoundError:
        return versions
    for requirement in required:
        if "extra ==" in requirement:
            continue
        found = re.match(r"[A-Za-z0-9_.-]+", requirement)
        if found is None:
            continue
        try:
            versions[found.group()] = metadata.version(found.group())
        except metadata.PackageNotFoundError:
            versions[found.group()] = "not installed"
    return versions


def system_info() -> dict[str, Any]:
    ffmpeg_path: str | None
    ffprobe_path: str | None
    try:
        ffmpeg, ffprobe = find_ffmpeg()
        ffmpeg_version = _run([str(ffmpeg), "-version"])
        ffmpeg_path, ffprobe_path = str(ffmpeg), str(ffprobe)
    except FfmpegError as exc:
        ffmpeg_version = str(exc)
        ffmpeg_path = ffprobe_path = None
    return {
        "os": _os_name(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "memory": _memory(),
        "python": sys.version,
        "mcrawsome": __version__,
        "dependencies": _dependencies(),
        "ffmpeg": ffmpeg_version,
        "ffmpeg_path": ffmpeg_path,
        "ffprobe_path": ffprobe_path,
    }


NOT_RECORDED = "not recorded"
LEFT_OUT = "recorded (left out of this bundle)"


def _clip_document(clip: Path) -> dict[str, Any]:
    """The clip's `info --json` description, with where it was recorded left out."""
    try:
        document = describe_any(clip)
    except Exception as exc:  # a clip that cannot be read is worth reporting too
        return {"clip": str(clip), "error": f"cannot read the clip ({exc})"}
    for section in document.values():
        if isinstance(section, dict) and section.get("Location", NOT_RECORDED) != NOT_RECORDED:
            section["Location"] = LEFT_OUT
    return document


def _json(document: Any) -> str:
    return json.dumps(document, indent=2, default=str) + "\n"


def bundle_members(items: list[Path]) -> dict[str, str]:
    """Every member but the README, by name, home folder hidden and logs cut."""
    members = {
        "doctor.json": _json({"checks": [c.model_dump(mode="json") for c in run_checks()]}),
        "system.json": _json(system_info()),
    }
    item_names = [
        f"sidecars/{item.name}"
        if item.name.lower().endswith(_SIDECAR_SUFFIXES)
        else f"clips/{item.name}.json"
        for item in items
    ]
    for index, (item, name) in enumerate(zip(items, item_names, strict=True), start=1):
        if item_names.count(name) > 1:
            category, filename = name.split("/", 1)
            name = f"{category}/{index:04d}/{filename}"
        if item.name.lower().endswith(_SIDECAR_SUFFIXES):
            members[name] = _tail(item.read_text(errors="replace"))
        else:
            members[name] = _json(_clip_document(item))
    return {name: _tail(_private(text)) for name, text in members.items()}


def make_bundle(out: Path, items: list[Path]) -> list[str]:
    """Write the support bundle to `out` (replacing it); the member names, README first."""
    members = bundle_members(items)
    names = ["README.txt", *members]
    readme = README.format(members="".join(f"  {name}\n" for name in names))
    part = out.with_name(f".{out.name}.part")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", readme)
            for name, text in members.items():
                archive.writestr(name, text)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
    return names
