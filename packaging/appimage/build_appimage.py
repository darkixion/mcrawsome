#!/usr/bin/python3 -I
"""Build mcrawsome-VERSION-REVISION-x86_64.AppImage on Debian 13, for Linux systems without the .deb.
Never installs system packages.

The image carries what it needs: a portable Python (python-build-standalone), the Python packages
pinned in uv.lock, a private FFmpeg that only decodes (build_ffmpeg.sh), and the libraries the window
uses (GTK 3, WebKitGTK, GStreamer), copied from this host with their Debian copyright notices. It runs
where glibc is 2.39 or later (Ubuntu 24.04, Linux Mint 22, Fedora 40 and later; audit_appdir.py fails the
build otherwise). It shares the .deb's revision number, so the two of a release can be told apart.

Usage: packaging/appimage/build_appimage.py OUTPUT-DIR [--sources]

--sources also writes the two archives a public release must offer beside the image: the FFmpeg and
dav1d sources with these build materials, and the Debian source of every bundled system package
(the LGPL components' corresponding source; deb-src lines must be enabled in apt). Downloads, the
AppDir and work files stay under ~/.cache/mcrawsome-appimage (MCRAWSOME_APPIMAGE_CACHE to change).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "packaging/appimage"
PYTHON_VERSION = "python3.12"
FFMPEG = Path.home() / ".cache/mcrawsome-ffmpeg/decode-prefix"
TOOLS = ("uv", "npm", "cc", "c++", "cmake", "ninja", "pkg-config", "strip", "ldd", "objdump", "dpkg-query",
         "glib-compile-schemas", "update-mime-database", "desktop-file-validate")  # fmt: skip
DEV_PACKAGES = {"cairo": "libcairo2-dev", "girepository-2.0": "libgirepository-2.0-dev"}
# Immutable releases, checked against the publishers' SHA-256 digests.
DOWNLOADS = {
    "python-stripped.tar.gz": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260414/"
        "cpython-3.12.13%2B20260414-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "3c3427e5628648478da2aa227472c350475a68bc58109f1b43849636a4aecb89",
    ),
    "python-build-standalone-source.tar.gz": (
        "https://github.com/astral-sh/python-build-standalone/archive/refs/tags/20260414.tar.gz",
        "a9e21da5b91bfac1f70888f37ba751dba7cb6bb7cd50e42b2b14409d55b7cf2e",
    ),
    "appimagetool.AppImage": (
        "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage",
        "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0",
    ),
    "runtime-x86_64": (
        "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64",
        "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d",
    ),
    "AppImage-runtime-MIT.txt": (
        "https://raw.githubusercontent.com/AppImage/type2-runtime/dd6cebedcbddde9c82f89b011e8e1d40b6e43868/LICENSE",
        "aa154fc9070614bbe7921f89db11efd1dba7a1f3a41685958110e2230f9c0ca1",
    ),
}


def run(command: list[str | Path], **options: object) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.run([str(part) for part in command], check=True, text=True, **options)  # type: ignore[call-overload,no-any-return]


def check_host() -> None:
    if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
        raise SystemExit("Linux x86_64 is required")
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    missing += [package for module, package in DEV_PACKAGES.items()
                if subprocess.run(["pkg-config", "--exists", module]).returncode]  # fmt: skip
    if missing:
        raise SystemExit(f"Missing build prerequisites: {', '.join(missing)}")


def fetch(downloads: Path, name: str) -> Path:
    url, digest = DOWNLOADS[name]
    path = downloads / name
    if not path.is_file():
        part = path.with_name(path.name + ".part")
        print("+ fetch", url, flush=True)
        with urllib.request.urlopen(url) as response, part.open("wb") as output:
            shutil.copyfileobj(response, output)
        part.rename(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"Checksum mismatch: {path}")
    return path


def _ffmpeg_inputs() -> str:
    digest = hashlib.sha256()
    for name in ("build_ffmpeg.sh", "ffmpeg-configure.txt"):
        digest.update((HERE / name).read_bytes())
    return digest.hexdigest()


def ffmpeg_current(prefix: Path) -> bool:
    """Whether the private FFmpeg in `prefix` was built from today's build script and options."""
    try:
        recorded = (prefix / "share/mcrawsome/ffmpeg-inputs.sha256").read_text().strip()
    except OSError:
        return False
    return (prefix / "bin/ffmpeg").is_file() and recorded == _ffmpeg_inputs()


def record_ffmpeg_inputs(prefix: Path) -> None:
    stamp = prefix / "share/mcrawsome/ffmpeg-inputs.sha256"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(_ffmpeg_inputs() + "\n")


def revision() -> str:
    """The .deb's revision: the two packages of a release share it."""
    found = re.search(
        r'^DEBIAN_REVISION = "(\d+)"$', (ROOT / "packaging/deb/build_deb.py").read_text(), re.MULTILINE
    )
    assert found is not None
    return found.group(1)


def main() -> None:
    arguments = sys.argv[1:]
    sources = "--sources" in arguments
    if sources:
        arguments.remove("--sources")
    if len(arguments) != 1:
        raise SystemExit(__doc__)
    out = Path(arguments[0]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    version = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
    assert version is not None
    name = f"mcrawsome-{version.group(1)}-{revision()}-x86_64"
    image = out / f"{name}.AppImage"
    if image.exists():
        raise SystemExit(f"Refusing to overwrite {image}; increment DEBIAN_REVISION")
    check_host()
    cache = Path(os.environ.get("MCRAWSOME_APPIMAGE_CACHE", Path.home() / ".cache/mcrawsome-appimage"))
    downloads, work = cache / "downloads", cache / "work"
    for folder in (downloads, work):
        folder.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, TMPDIR=str(work), CMAKE_BUILD_PARALLEL_LEVEL="4", MAKEFLAGS="-j4")
    paths = {name: fetch(downloads, name) for name in DOWNLOADS}
    run(["/usr/bin/python3", "-I", ROOT / "scripts/build_web.py"], env=environment)
    if not ffmpeg_current(FFMPEG):
        run([HERE / "build_ffmpeg.sh", FFMPEG], env=environment)
        record_ffmpeg_inputs(FFMPEG)
    run(["uv", "run", "--project", ROOT, "python", HERE / "audit_ffmpeg.py", FFMPEG / "bin/ffmpeg",
         FFMPEG / "bin/ffprobe"], env=environment)  # fmt: skip

    appdir = cache / "mcrawsome.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    for folder in ("usr/bin", "usr/lib", "usr/python"):
        (appdir / folder).mkdir(parents=True)
    with tarfile.open(paths["python-stripped.tar.gz"]) as archive:
        archive.extractall(work / "python", filter="data")
    shutil.copytree(work / "python/python", appdir / "usr/python", dirs_exist_ok=True, symlinks=True)
    shutil.rmtree(work / "python")
    python = appdir / "usr/python/bin/python3"
    # pip is only a build tool: its vendored libraries must not become runtime dependencies.
    run([python, "-m", "pip", "uninstall", "-y", "pip"], env=environment)
    requirements = work / "requirements.txt"
    run(["uv", "export", "--quiet", "--project", ROOT, "--frozen", "--extra", "gui", "--no-dev",
         "--no-emit-project", "--output-file", requirements], env=environment)  # fmt: skip
    run(["uv", "pip", "install", "--python", python, "-r", requirements], env=environment)
    # Built for the bundled Python, into empty folders, so nothing from another build is installed.
    wheels, native = work / "wheels", work / "native-wheel"
    shutil.rmtree(wheels, ignore_errors=True)
    shutil.rmtree(native, ignore_errors=True)
    run(["uv", "build", "--project", ROOT, "--python", python, "--wheel", "--out-dir", wheels,
         "--config-setting", f"build-dir={native}"], env=environment)  # fmt: skip
    [wheel] = wheels.glob("mcrawsome-*.whl")
    run(["uv", "pip", "install", "--python", python, "--no-deps", "--reinstall", wheel], env=environment)
    site = appdir / "usr/python/lib" / PYTHON_VERSION / "site-packages"
    if not any((site / "mcrawsome").glob("_fuse.*.so")):
        raise SystemExit("The wheel has no FUSE adapter: install libfuse3-dev and build again")
    licences = site / "mcrawsome/packaging/LICENCES"
    for program in ("ffmpeg", "ffprobe"):
        shutil.copy2(FFMPEG / "bin" / program, appdir / "usr/bin" / program)
    for notice in (FFMPEG / "share/mcrawsome/LICENCES").iterdir():
        shutil.copy2(notice, licences / notice.name)
    shutil.copy2(paths["AppImage-runtime-MIT.txt"], licences / "AppImage-runtime-MIT.txt")
    run([python, HERE / "bundle.py", appdir, ROOT, cache], env=environment)
    built = FFMPEG / "share/mcrawsome/ffmpeg-manifest.toml"
    shutil.copy2(built, appdir / "usr/share/mcrawsome/ffmpeg-manifest.toml")
    gio = run(["pkg-config", "--cflags", "--libs", "gio-2.0"], capture_output=True, env=environment)
    flags = gio.stdout.split()
    run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", HERE / "webkit-launcher.c", "-o",
         appdir / "usr/lib/mcrawsome-webkit-launcher.so", *flags, "-ldl"], env=environment)  # fmt: skip
    linux = ROOT / "packaging/linux"
    for source in (HERE / "AppRun", linux / "mcrawsome.desktop", linux / "mcrawsome.svg"):
        shutil.copy2(source, appdir / source.name)
    (appdir / ".DirIcon").symlink_to("mcrawsome.svg")
    run(["desktop-file-validate", appdir / "mcrawsome.desktop"], env=environment)
    with (work / "prune-report.json").open("w") as report:
        run([python, HERE / "prune_runtime.py", appdir], stdout=report, env=environment)
    run([python, HERE / "audit_appdir.py", appdir], env=environment)
    if sources:
        archives(appdir, cache, out, name, environment)
    tool = work / "squashfs-root/AppRun"
    if not tool.is_file():
        paths["appimagetool.AppImage"].chmod(0o755)
        run([paths["appimagetool.AppImage"], "--appimage-extract"], cwd=work, stdout=subprocess.DEVNULL)
    part = image.with_name(image.name + ".part")
    squashfs = ["-processors", "4", "-b", "524288", "-Xcompression-level", "19"]
    run(["nice", "-n", "19", tool, "--runtime-file", paths["runtime-x86_64"], "--comp", "zstd",
         *(f"--mksquashfs-opt={option}" for option in squashfs), appdir, part],
        env=dict(environment, ARCH="x86_64"))  # fmt: skip
    part.rename(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    image.with_name(image.name + ".sha256").write_text(f"{digest}  {image.name}\n")
    print(f"Built {image} ({image.stat().st_size} bytes)")


def archives(appdir: Path, cache: Path, out: Path, name: str, environment: dict[str, str]) -> None:
    """The release's source archives: FFmpeg and dav1d with the build materials, then the Debian source
    of every bundled system package (about 800 MB, already compressed, so stored as they are)."""
    sources = cache / "sources"
    shutil.rmtree(sources / "ffmpeg", ignore_errors=True)
    shutil.rmtree(sources / "build-materials", ignore_errors=True)
    run(["uv", "run", "--project", ROOT, "python", HERE / "source_archives.py",
         FFMPEG / "share/mcrawsome/ffmpeg-manifest.toml", sources / "ffmpeg"], env=environment)  # fmt: skip
    shutil.copytree(HERE, sources / "build-materials", ignore=shutil.ignore_patterns("__pycache__"))
    manifest = appdir / "usr/share/mcrawsome/runtime-manifest.json"
    run(["uv", "run", "--project", ROOT, "python", HERE / "system_sources.py", manifest, sources / "system"],
        env=environment)  # fmt: skip
    with tarfile.open(out / f"{name}-sources.tar.xz", "w:xz") as archive:
        for folder in ("ffmpeg", "build-materials"):
            archive.add(sources / folder, arcname=f"sources/{folder}")
    with tarfile.open(out / f"{name}-system-sources.tar", "w") as archive:
        archive.add(sources / "system", arcname="sources/system")


if __name__ == "__main__":
    main()
