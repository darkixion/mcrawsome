#!/usr/bin/python3 -I
"""Build mcrawsome_VERSION_amd64.deb from this checkout for Debian 13. Never installs system packages.

MCRAWsome uses Debian's own Python, FFmpeg, WebKitGTK, GTK, GStreamer and Python packages. Only the
Python packages Debian lacks, or has older than MCRAWsome needs, are vendored, at the uv.lock versions,
into /usr/lib/mcrawsome/vendor. The Debian packages the build host has become Depends, at least at the
versions installed there, which are the versions this build was resolved (and should be tested) with.

Usage: packaging/deb/build_deb.py OUTPUT-DIR
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
DEBIAN_REVISION = "1"
PYTHON = "/usr/bin/python3"
DIST_PACKAGES = "usr/lib/python3/dist-packages"
VENDOR = "usr/lib/mcrawsome/vendor"
TOOLS = ("uv", "npm", "cmake", "c++", "dpkg-deb", "dpkg-query", "ldd")
# Needed to build: the C++ modules' headers, the venv used to resolve, and the Debian packages that
# provide MCRAWsome's requirements. Without the latter the resolver would vendor them instead.
BUILD_PACKAGES = (
    "python3-dev",
    "python3-venv",
    "python3-packaging",
    "libfuse3-dev",
    "python3-numpy",
    "python3-scipy",
    "python3-opencv",
    "python3-pydantic",
    "python3-click",
    "python3-rich",
    "python3-tomli-w",
    "python3-websockets",
    "python3-webview",
    "python3-gi",
    "python3-gi-cairo",
)
# Requirements Debian satisfies although pip cannot tell: its OpenCV has no opencv-python-headless
# metadata but provides the same cv2 module, and its pywebview still lists proxy-tools, which Debian
# removed from the code. Each is checked by importing its module and comparing the version.
ALIASES = {
    "opencv-python-headless": ("python3-opencv", "cv2"),
    "pywebview": ("python3-webview", "webview"),
}
# Used at run time but not by Python imports: the window, its media, FUSE, FFmpeg and xdg-open, the time
# zones of time-of-day timecode, and the GNOME location data places are named from.
RUNTIME = (
    "systemd",
    "tzdata",
    "libgweather-4-common",
    "dbus-user-session",
    "ffmpeg",
    "fuse3",
    "xdg-utils",
    "python3-gi",
    "python3-gi-cairo",
    "python3-nautilus",
    "gir1.2-gtk-3.0",
    "gir1.2-webkit2-4.1",
    "gir1.2-gstreamer-1.0",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
)
# The GTK 4 window (MCRAWSOME_GTK=4, a prototype): recommended, so apt installs them by default, but not
# required, and the build host need not have them; without them the GTK 3 window opens.
RECOMMENDS = ("gir1.2-gtk-4.0", "gir1.2-webkit-6.0")
LICENCE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)


def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, check=True, text=True, **options)  # type: ignore[call-overload,no-any-return]


def installed_version(package: str) -> str | None:
    result = subprocess.run(
        ["dpkg-query", "--showformat=${db:Status-Abbrev}${Version}", "--show", f"{package}:amd64"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["dpkg-query", "--showformat=${db:Status-Abbrev}${Version}", "--show", package],
            capture_output=True,
            text=True,
        )
    status, _, version = result.stdout.partition(" ")
    return version.strip() if result.returncode == 0 and status.startswith("ii") else None


def owner(path: Path) -> str:
    """The Debian package that installed `path`, trying the merged-/usr spelling too."""
    candidates = [path, path.resolve()]
    for candidate in list(candidates):
        text = str(candidate)
        if text.startswith("/usr/lib/"):
            candidates.append(Path(text.removeprefix("/usr")))
        elif text.startswith("/lib"):
            candidates.append(Path("/usr" + text))
    for candidate in candidates:
        result = subprocess.run(["dpkg-query", "--search", str(candidate)], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.split(":")[0].split(",")[0].strip()
    raise SystemExit(f"No Debian package owns {path}; install it from Debian or vendor it")


def check_host() -> None:
    missing_tools = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing_tools:
        raise SystemExit(f"Missing build tools: {' '.join(missing_tools)}")
    missing = [
        package
        for package in BUILD_PACKAGES + tuple(p for p in RUNTIME if p != "python3-nautilus")
        if installed_version(package) is None
    ]
    if missing:
        raise SystemExit(f"Install the build prerequisites first:\n  sudo apt install {' '.join(missing)}")


def lock_versions() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return {canonicalize_name(package["name"]): package["version"] for package in lock["package"]}


def requirements() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    wanted = [Requirement(text) for text in project["dependencies"] + project["optional-dependencies"]["gui"]]
    # GTK and GI are explicit Debian runtime dependencies. Do not resolve the pip GTK extra:
    # it adds typing stubs pinned to a different PyGObject than Debian's runtime.
    for requirement in wanted:
        if canonicalize_name(requirement.name) == "pywebview":
            requirement.extras.clear()
    return wanted


def resolve(venv: Path, work: Path, wanted: list[str], constraints: Path | None) -> dict[str, str]:
    """What pip would add to Debian's packages for `wanted`: canonical name to version."""
    report = work / "report.json"
    # proxy-tools is a small pure-Python dependency of pywebview with only an sdist.
    command = [
        str(venv / "bin/pip"),
        "install",
        "--quiet",
        "--dry-run",
        "--only-binary=:all:",
        "--no-binary=proxy-tools",
    ]
    command += [
        "--report",
        str(report),
        *(["--constraint", str(constraints)] if constraints else []),
        *wanted,
    ]
    run(command)
    chosen = json.loads(report.read_text())["install"]
    return {canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"] for item in chosen}


def vendor(venv: Path, work: Path) -> dict[str, str]:
    environment = {name: str(value) for name, value in default_environment().items()}
    wanted: list[str] = []
    for requirement in requirements():
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        alias = ALIASES.get(canonicalize_name(requirement.name))
        if alias is not None:
            module = alias[1]
            probe = (
                f"import importlib.metadata as m, {module}\n"
                f"try: print(m.version({requirement.name!r}))\n"
                f"except m.PackageNotFoundError: print({module}.__version__)"
            )
            version = run([PYTHON, "-I", "-c", probe], capture_output=True).stdout.strip()
            if requirement.specifier.contains(version, prereleases=True):
                continue  # provided by Debian under another name
        wanted.append(str(requirement))
    first = resolve(venv, work, wanted, None)
    # The tested versions: uv.lock's, wherever it has the package.
    locked = lock_versions()
    pins = {name: locked.get(name, version) for name, version in first.items()}
    constraints = work / "constraints.txt"
    constraints.write_text("".join(f"{name}=={version}\n" for name, version in sorted(pins.items())))
    second = resolve(venv, work, wanted, constraints)
    if set(second) != set(first):
        raise SystemExit(f"Pinning to uv.lock changed what is vendored: {sorted(set(first) ^ set(second))}")
    return second


def python_depends(stage: Path, vendored: dict[str, str]) -> set[str]:
    """Debian packages providing the requirements of MCRAWsome and its vendored packages, transitively."""
    paths = [str(stage / VENDOR), *[p for p in sys.path if p.startswith("/usr/lib/python3")]]
    found: dict[str, metadata.Distribution] = {}
    for distribution in metadata.distributions(path=paths):  # vendored first, as at run time
        found.setdefault(canonicalize_name(distribution.metadata["Name"]), distribution)
    environment = {name: str(value) for name, value in default_environment().items()}
    packages: set[str] = set()
    seen: set[str] = set()
    pending = [r for r in requirements() if r.marker is None or r.marker.evaluate(environment)]
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        alias = ALIASES.get(name)
        if alias is not None and name not in vendored:
            packages.add(alias[0])
            continue
        for extra in requirement.extras or {""}:
            key = f"{name}[{extra}]"
            if key in seen:
                continue
            seen.add(key)
            provider = found.get(name)
            if provider is None:
                raise SystemExit(f"{name} is neither vendored nor installed from Debian")
            if name not in vendored:
                # Debian's packages often ship no RECORD, so ask dpkg about the metadata folder itself.
                packages.add(owner(Path(getattr(provider, "_path"))))  # noqa: B009
            for text in provider.requires or []:
                child = Requirement(text)
                scope = dict(environment, extra=extra)
                if child.marker is None or child.marker.evaluate(scope):
                    pending.append(child)
    return packages


def elf_depends(stage: Path) -> set[str]:
    """Debian packages of the shared libraries that the built and vendored modules load from the system."""
    packages: set[str] = set()
    for path in stage.rglob("*.so*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as file:
            if file.read(4) != b"\x7fELF":
                continue
        output = run(["ldd", str(path)], capture_output=True).stdout
        if "not found" in output:
            raise SystemExit(f"Unresolved library for {path}:\n{output}")
        for location in re.findall(r"=> (/\S+)", output):
            if not Path(location).is_relative_to(stage):
                packages.add(owner(Path(location)))
    return packages


def licences(stage: Path, vendored: dict[str, str]) -> None:
    """Each vendored package's licence files, listed with MCRAWsome's own on the About screen."""
    folder = stage / DIST_PACKAGES / "mcrawsome/packaging/LICENCES"
    for distribution in metadata.distributions(path=[str(stage / VENDOR)]):
        name = canonicalize_name(distribution.metadata["Name"])
        if name not in vendored:
            continue
        texts = [
            Path(str(distribution.locate_file(file))).read_text(errors="replace")
            for file in distribution.files or []
            if ".dist-info" in str(file) and LICENCE.match(Path(str(file)).name)
        ]
        if not texts:
            texts.append(
                distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or ""
            )
        (folder / f"Python-{name}.txt").write_text(f"{name} {vendored[name]}\n\n" + "\n\n".join(texts))


def floor(package: str) -> str:
    version = installed_version(package)
    assert version is not None, package
    return f"{package} (>= {version})"


def launcher_source() -> str:
    return f'#!{PYTHON}\nfrom mcrawsome.cli.main import main\n\nif __name__ == "__main__":\n    main()\n'


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    destination = out / f"mcrawsome_{project_version}-{DEBIAN_REVISION}_amd64.deb"
    if destination.exists():
        raise SystemExit(f"Refusing to overwrite {destination}; increment DEBIAN_REVISION")
    check_host()
    major, minor = sys.version_info[:2]
    with tempfile.TemporaryDirectory(dir=out, prefix=".deb-build-") as folder:
        work = Path(folder)
        stage = work / "stage"
        run([PYTHON, "-I", str(ROOT / "scripts/build_web.py")])
        env = dict(os.environ, CMAKE_BUILD_PARALLEL_LEVEL=os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL", "4"))
        run(
            [
                "uv",
                "build",
                "--project",
                str(ROOT),
                "--python",
                PYTHON,
                "--wheel",
                "--out-dir",
                str(work / "wheels"),
                "--config-setting",
                f"build-dir={work / 'native'}",
            ],
            env=env,
        )
        [wheel] = (work / "wheels").glob("mcrawsome-*.whl")
        run([PYTHON, "-I", "-m", "venv", "--system-site-packages", str(work / "venv")])
        venv = work / "venv"
        vendored = vendor(venv, work)
        pip = [
            str(venv / "bin/pip"),
            "install",
            "--quiet",
            "--no-deps",
            "--no-compile",
            "--only-binary=:all:",
            "--no-binary=proxy-tools",
        ]
        run([*pip, "--target", str(stage / DIST_PACKAGES), str(wheel)])
        if vendored:
            run([*pip, "--target", str(stage / VENDOR), *(f"{n}=={v}" for n, v in sorted(vendored.items()))])
        for scripts in (stage / DIST_PACKAGES / "bin", stage / VENDOR / "bin"):
            shutil.rmtree(scripts, ignore_errors=True)
        for record in stage.rglob("*.dist-info/direct_url.json"):  # names the build folder
            record.unlink()
        package = stage / DIST_PACKAGES / "mcrawsome"
        if not any(package.glob("_fuse.*.so")):
            raise SystemExit("The wheel has no FUSE adapter: install libfuse3-dev and build again")
        (package / "VENDOR").write_text(f"/{VENDOR}\n")
        version = run(
            [
                PYTHON,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import mcrawsome; print(mcrawsome.__version__)",
                str(stage / DIST_PACKAGES),
            ],
            capture_output=True,
        ).stdout.strip()
        licences(stage, vendored)
        # Service snapshots use this content identity, independent of package upgrade paths.
        digest = hashlib.sha256()
        for tree in (package, stage / VENDOR):
            for item in sorted(tree.rglob("*")):
                if item.is_file():
                    digest.update(str(item.relative_to(stage)).encode() + b"\0")
                    digest.update(item.read_bytes())
        (package / "MOUNT_RUNTIME").write_text(
            json.dumps({"id": digest.hexdigest(), "protocol": 1, "vendor": f"/{VENDOR}"}) + "\n"
        )

        launcher = stage / "usr/bin/mcrawsome"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(launcher_source())
        launcher.chmod(0o755)
        share = stage / "usr/share"
        linux = ROOT / "packaging/linux"
        for source, target in (
            (linux / "mcrawsome.desktop", share / "applications/mcrawsome.desktop"),
            (linux / "mcrawsome.svg", share / "icons/hicolor/scalable/apps/mcrawsome.svg"),
            (
                linux / "mcrawsome.svg",
                share / "icons/hicolor/scalable/mimetypes/application-x-mcrawsome-session.svg",
            ),
            (linux / "mcrawsome-mime.xml", share / "mime/packages/mcrawsome.xml"),
            (linux / "mcrawsome.metainfo.xml", share / "metainfo/mcrawsome.metainfo.xml"),
            # GNOME Shell searches recordings through MCRAWsome, which D-Bus starts when asked.
            (linux / "mcrawsome-search-provider.ini", share / "gnome-shell/search-providers/mcrawsome.ini"),
            (
                linux / "io.github.darkixion.mcrawsome.SearchProvider.service",
                share / "dbus-1/services/io.github.darkixion.mcrawsome.SearchProvider.service",
            ),
            # File managers' thumbnails of .mcraw recordings (thumbnailer.py); not in the AppImage, which
            # GNOME's sandbox for thumbnailers cannot reach.
            (linux / "mcrawsome.thumbnailer", share / "thumbnailers/mcrawsome.thumbnailer"),
            (
                ROOT / "packaging/file-managers/nautilus/mcrawsome_menu.py",
                share / "nautilus-python/extensions/mcrawsome_menu.py",
            ),
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for extension in ("mcrawsome_properties.py", "mcrawsome_columns.py"):
            shutil.copyfile(
                ROOT / "packaging/file-managers/nautilus" / extension,
                share / "nautilus-python/extensions" / extension,
            )
        # Emblems the Files extension puts on recordings mounted for, or open in, DaVinci Resolve.
        emblems = share / "icons/hicolor/scalable/emblems"
        emblems.mkdir(parents=True, exist_ok=True)
        for icon in sorted((linux / "emblems").glob("*.svg")):
            shutil.copyfile(icon, emblems / icon.name)
        doc = share / "doc/mcrawsome"
        doc.mkdir(parents=True)
        listed = "".join(f"\n {name} {version}" for name, version in sorted(vendored.items())) or "\n (none)"
        (doc / "copyright").write_text(
            "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
            "Upstream-Name: MCRAWsome\n"
            "Comment: These Python packages are vendored in /usr/lib/mcrawsome/vendor, because Debian lacks\n"
            " them or has older versions; their licences are in\n"
            f" /{DIST_PACKAGES}/mcrawsome/packaging/LICENCES:{listed}\n\n"
            "Files: *\nCopyright: Thom Brown\nLicense: Apache-2.0\n"
            " On Debian systems the full text is in /usr/share/common-licenses/Apache-2.0.\n"
        )

        depends = {f"python3 (>= {major}.{minor})", f"python3 (<< {major}.{minor + 1})"}
        depends |= {
            (name if name == "python3-nautilus" else floor(name))
            for name in python_depends(stage, vendored) | elf_depends(stage) | set(RUNTIME)
        }
        control = stage / "DEBIAN"
        control.mkdir()
        lifecycle = ROOT / "packaging/deb/package_lifecycle.py"
        helper = stage / "usr/lib/mcrawsome/package_lifecycle.py"
        shutil.copyfile(lifecycle, helper)
        # Preinst also handles migration from an AppImage or an older deb with no stop hook.
        shutil.copyfile(lifecycle, control / "preinst")
        # Byte-compile on install, as dh_python3 would, and remove the caches on removal.
        (control / "postinst").write_text(
            f"#!/bin/sh\nset -e\npy3compile -p mcrawsome\npy3compile /{VENDOR}\n"
            'if [ "$1" = configure ] || [ "$1" = abort-upgrade ] || [ "$1" = abort-remove ]; then\n'
            "  /usr/bin/python3 -I /usr/lib/mcrawsome/package_lifecycle.py configure\nfi\n"
        )
        (control / "prerm").write_text(
            '#!/bin/sh\nset -e\n/usr/bin/python3 -I /usr/lib/mcrawsome/package_lifecycle.py "$@"\n'
            f"py3clean -p mcrawsome\npy3clean /{VENDOR}\n"
        )
        for script in ("preinst", "postinst", "prerm"):
            (control / script).chmod(0o755)
        stage.chmod(0o755)
        for path in stage.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        size = (
            sum(p.stat().st_size for p in stage.rglob("*") if p.is_file() and control not in p.parents)
            // 1024
        )
        (control / "control").write_text(
            f"Package: mcrawsome\nVersion: {version}-{DEBIAN_REVISION}\nArchitecture: amd64\n"
            "Maintainer: Thom Brown <thom@linux.com>\n"
            f"Pre-Depends: python3 (>= {major}.{minor}), python3 (<< {major}.{minor + 1})\n"
            f"Installed-Size: {size}\nDepends: {', '.join(sorted(depends))}\n"
            f"Recommends: {', '.join(RECOMMENDS)}\n"
            "Section: video\nPriority: optional\n"
            "Homepage: https://github.com/darkixion/mcrawsome\n"
            "Description: Develop MCRAW recordings for DaVinci Resolve\n"
            " MCRAWsome is an independent open-source application for MCRAW RAW video\n"
            " recorded with MotionCam Pro: preview and develop it, export CinemaDNG and\n"
            " OpenEXR, and send virtual CinemaDNG mounts to DaVinci Resolve. It is not\n"
            " affiliated with or endorsed by MotionCam.\n"
        )
        deb = out / f"mcrawsome_{version}-{DEBIAN_REVISION}_amd64.deb"
        run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(stage), str(deb)])
    print(f"Built {deb} ({deb.stat().st_size} bytes)")
    print(f"Vendored: {', '.join(f'{n} {v}' for n, v in sorted(vendored.items())) or 'nothing'}")


if __name__ == "__main__":
    main()
