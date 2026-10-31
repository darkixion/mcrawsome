"""Remove build assets and unused subpackages from the measured application runtime.

Keep calibration data, distribution metadata, notices and bytecode. The SciPy subset is reviewed
for exact dependency versions. Run after collecting the native wheel and dependency licences.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TypedDict


class Removed(TypedDict):
    path: str
    bytes: int


# Measured: none of these loads when MCRAWsome's SciPy imports do (ndimage, linalg, spatial.transform and
# signal, which brings in stats, integrate, optimize and interpolate). audit_python_runtime.py imports
# every SciPy name MCRAWsome uses from the pruned image, so a new one that needs them fails the build.
SCIPY_UNUSED = ("cluster", "datasets", "differentiate", "fftpack", "io", "misc", "odr")
SCIPY_PROFILE = {"scipy": "1.18.1"}


def scipy_candidates(site: Path) -> list[Path]:
    if not (site / "scipy").is_dir():
        return []
    versions = {
        item.metadata.get("Name", "").lower().replace("_", "-"): item.version
        for item in importlib.metadata.distributions(path=[str(site)])
        if item.metadata.get("Name")
    }
    for name, version in SCIPY_PROFILE.items():
        if versions.get(name) != version:
            raise ValueError(f"{name} {versions.get(name)}: review the SciPy runtime profile before pruning")
    return [site / "scipy" / name for name in SCIPY_UNUSED]


def strip_libraries(site: Path) -> list[Removed]:
    stripped: list[Removed] = []
    for path in sorted(site.rglob("*")):
        if path.is_symlink() or not path.is_file() or not (path.name.endswith(".so") or ".so." in path.name):
            continue
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                continue
        # uv may hard-link wheels from its shared cache. Never strip that inode in place.
        temporary = path.with_name(f".{path.name}.strip-{uuid.uuid4().hex}")
        before = path.stat().st_size
        try:
            shutil.copy2(path, temporary)
            subprocess.run(["strip", "--strip-unneeded", str(temporary)], check=True, capture_output=True)
            after = temporary.stat().st_size
            if after < before:
                os.replace(temporary, path)
                stripped.append({"path": str(path.relative_to(site)), "bytes": before - after})
        finally:
            temporary.unlink(missing_ok=True)
    return stripped


def prune(appdir: Path) -> dict[str, object]:
    python = appdir / "usr/python"
    if not python.is_dir():
        raise ValueError("No bundled Python runtime in this AppDir")
    library = python / "lib/python3.12"
    site = library / "site-packages"
    candidates = [
        *scipy_candidates(site),
        python / "include",
        library / "test",
        library / "ensurepip",
        library / "idlelib",
        library / "tkinter",
        library / "turtledemo",
        library / "turtle.py",
        library / "lib2to3",
        site / "gi-stubs",
    ]
    candidates.extend((python / "lib").glob("libpython*.a"))
    candidates.extend((library / "__pycache__").glob("turtle.*.pyc"))
    for package in ("numpy", "scipy"):
        candidates.extend((site / package).rglob("tests"))
    removed = []
    total = 0
    for path in sorted(set(candidates), key=lambda item: (len(item.parts), str(item))):
        if path.is_symlink():
            continue
        if not path.exists():
            continue
        size = (
            sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())
            if path.is_dir()
            else path.stat().st_size
        )
        removed.append({"path": str(path.relative_to(appdir)), "bytes": size})
        total += size
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    stripped = strip_libraries(site)
    return {
        "removed_bytes": total,
        "removed": removed,
        "stripped_bytes": sum(item["bytes"] for item in stripped),
        "stripped": stripped,
    }


if __name__ == "__main__":
    print(json.dumps(prune(Path(sys.argv[1])), indent=2))
