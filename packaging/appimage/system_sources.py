"""Download and check the Debian source of every system package bundled in the AppImage.

Usage: system_sources.py MANIFEST FOLDER [--check-only]

MANIFEST is runtime-manifest.json, written by bundle.py. Each bundled binary package is mapped
to its source package and version with dpkg-query, the sources are fetched with
`apt-get source --download-only` (a file already present and verified is not fetched again), files
no longer referenced are removed, and every .dsc and the files it lists are checked by SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

Expected = dict[tuple[str, str], list[str]]
LISTING = "SOURCES.txt"


def dpkg_query(packages: list[str]) -> str:
    return subprocess.check_output(
        [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n",
            *packages,
        ],
        text=True,
    )


def _plain(package: str) -> str:
    return package.split(":", 1)[0]


def expected_sources(manifest: dict[str, str], query: Callable[[list[str]], str] = dpkg_query) -> Expected:
    """{(source package, source version): [bundled binary packages]} for the manifest's packages, which
    must still be installed at the versions bundled."""
    binaries = {
        _plain(name): (name, version)
        for name, version in manifest.items()
        if not name.startswith(("usr/", "Python-"))
    }
    expected: Expected = {}
    for line in query([name for name, _ in binaries.values()]).splitlines():
        binary, version, source, source_version = line.split("\t")
        name, bundled = binaries.pop(_plain(binary))
        if version != bundled:
            raise SystemExit(f"{name} is now {version}, but {bundled} was bundled: build again")
        expected.setdefault((source, source_version), []).append(name)
    if binaries:
        raise SystemExit(f"Not installed: {', '.join(sorted(binaries))}")
    return {key: sorted(names) for key, names in sorted(expected.items())}


def dsc_name(source: str, version: str) -> str:
    return f"{source}_{version.split(':', 1)[-1]}.dsc"


def _fields(dsc: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The .dsc's single-line fields and its Checksums-Sha256 {file: digest}."""
    fields: dict[str, str] = {}
    sums: dict[str, str] = {}
    current = ""
    for line in dsc.read_text(errors="replace").splitlines():
        if line.startswith("-----BEGIN PGP SIGNATURE"):
            break
        if line.startswith(" ") and current == "Checksums-Sha256":
            digest, _size, name = line.split()
            sums[name] = digest
        elif ":" in line and not line.startswith(" "):
            current, _, value = line.partition(":")
            fields[current] = value.strip()
    return fields, sums


def referenced(folder: Path, expected: Expected) -> set[str]:
    names = {LISTING}
    for source, version in expected:
        dsc = folder / dsc_name(source, version)
        names.add(dsc.name)
        if dsc.is_file():
            names.update(_fields(dsc)[1])
    return names


def check(folder: Path, expected: Expected) -> list[str]:
    """Problems: a missing or mismatched .dsc or source file, or a file no expected .dsc lists."""
    problems: list[str] = []
    for source, version in expected:
        dsc = folder / dsc_name(source, version)
        if not dsc.is_file():
            problems.append(f"{source} {version}: missing {dsc.name}")
            continue
        fields, sums = _fields(dsc)
        if (fields.get("Source"), fields.get("Version")) != (source, version):
            problems.append(f"{source} {version}: {dsc.name} describes another package")
        if not sums:
            problems.append(f"{source} {version}: {dsc.name} lists no files")
        for name, digest in sums.items():
            path = folder / name
            if not path.is_file():
                problems.append(f"{source} {version}: missing {name}")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                problems.append(f"{source} {version}: {name} does not match")
    known = referenced(folder, expected)
    problems += [
        f"unexpected file {path.name}" for path in sorted(folder.iterdir()) if path.name not in known
    ]
    return problems


def prune(folder: Path, expected: Expected) -> None:
    """Remove what an earlier build left: other versions, other packages, partial downloads."""
    known = referenced(folder, expected)
    for path in folder.iterdir():
        if path.name not in known:
            path.unlink()


def download(folder: Path, expected: Expected) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    wanted = [f"{source}={version}" for source, version in expected]
    subprocess.run(["apt-get", "source", "--only-source", "--download-only", *wanted], cwd=folder, check=True)
    prune(folder, expected)
    (folder / LISTING).write_text(
        "Debian source packages of the system libraries bundled in the AppImage.\n"
        "Unpack one with `dpkg-source -x FILE.dsc`.\n\n"
        + "".join(
            f"{source} {version}: {', '.join(names)}\n" for (source, version), names in expected.items()
        )
    )


def main(arguments: list[str]) -> int:
    manifest, folder = json.loads(Path(arguments[0]).read_text()), Path(arguments[1])
    expected = expected_sources(manifest)
    if "--check-only" not in arguments[2:]:
        download(folder, expected)
    problems = check(folder, expected)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    size = sum(path.stat().st_size for path in folder.iterdir())
    binaries = sum(map(len, expected.values()))
    print(f"{len(expected)} source packages for {binaries} binary packages, {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
