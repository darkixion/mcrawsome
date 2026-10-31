"""Collect the system runtime closure, typelibs and licences on a Debian-family build host.

Run with the AppDir (MCRAWsome and its Python packages already installed), the checkout and the build cache.
The licences go where the About screen lists them, the installed package's packaging/LICENCES, and the
manifest of bundled packages, versions and file hashes into usr/share/mcrawsome; nothing is written to the
checkout."""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
from pathlib import Path

APPDIR, ROOT, CACHE = map(Path, sys.argv[1:])
LIB = APPDIR / "usr/lib"
LICENCES = next((APPDIR / "usr/python/lib").glob("python3.*/site-packages/mcrawsome/packaging/LICENCES"))
SYSTEM = Path("/usr/lib/x86_64-linux-gnu")
# These belong to the host's dynamic loader and must remain together.
GLIBC = re.compile(r"^(ld-linux|lib(c|m|pthread|dl|rt|resolv|util|nss_[^.]+)\.so[.\d]*$)")
packages: set[str] = set()
queue: list[Path] = []
copied: set[Path] = set()
manifest: dict[str, str] = {}


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True)


# Read dpkg's installed file index once; one dpkg-query process per icon/library is slow.
owners: dict[str, str] = {}
for listing in Path("/var/lib/dpkg/info").glob("*.list"):
    for filename in listing.read_text().splitlines():
        owners[filename] = listing.stem


def owner(path: Path) -> None:
    if not str(path).startswith(("/usr/", "/lib/")):
        return
    choices = [path, path.resolve()]
    if str(path).startswith("/usr/lib/"):
        choices.append(Path(str(path).removeprefix("/usr")))
    for choice in choices:
        package = owners.get(str(choice))
        if package is not None:
            packages.add(package)
            return
    if str(path).startswith("/usr/share/mime/"):
        packages.add("shared-mime-info")
        return
    if path.name in {"gschemas.compiled", "icon-theme.cache", "giomodule.cache"}:
        return  # generated from the neighbouring, licensed source data
    raise SystemExit(f"Cannot identify the package licence for {path}")


def copy(path: Path, target: Path) -> None:
    if target in copied:
        return
    copied.add(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    shutil.copy2(path, target)
    owner(path)
    with path.open("rb") as source:
        if source.read(4) == b"\x7fELF":
            queue.append(path)
    manifest[str(target.relative_to(APPDIR))] = hashlib.sha256(target.read_bytes()).hexdigest()


def tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"Missing runtime directory: {source}")
    for path in sorted(source.rglob("*")):
        if path.is_file():
            copy(path, target / path.relative_to(source))


# GIRepository 2.0 changed the default typelib path. Use the build host's actual path,
# and copy only the namespaces used by GTK, WebKit and pywebview, including their dependencies.
os.environ["GI_TYPELIB_PATH"] = str(SYSTEM / "girepository-1.0")
gi = ctypes.CDLL("libgirepository-2.0.so.0")
gi.gi_repository_new.restype = ctypes.c_void_p
repo = gi.gi_repository_new()
gi.gi_repository_require.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_void_p,
]
gi.gi_repository_require.restype = ctypes.c_void_p
for namespace, version in ((b"Gtk", b"3.0"), (b"WebKit2", b"4.1"), (b"GdkX11", b"3.0"), (b"Gst", b"1.0")):
    if not gi.gi_repository_require(repo, namespace, version, 0, None):
        raise SystemExit(f"Missing system typelib: {namespace.decode()}-{version.decode()}")
gi.gi_repository_get_loaded_namespaces.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
gi.gi_repository_get_loaded_namespaces.restype = ctypes.POINTER(ctypes.c_char_p)
count = ctypes.c_size_t()
names = gi.gi_repository_get_loaded_namespaces(repo, ctypes.byref(count))
gi.gi_repository_get_typelib_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
gi.gi_repository_get_typelib_path.restype = ctypes.c_char_p
gi.gi_repository_get_shared_libraries.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.POINTER(ctypes.c_size_t),
]
gi.gi_repository_get_shared_libraries.restype = ctypes.POINTER(ctypes.c_char_p)
# ldconfig is in /sbin, which need not be on a regular user's PATH.
ldconfig = run("/sbin/ldconfig", "-p")
resolved = dict(re.findall(r"^\s*(\S+) \([^\n]*x86-64[^\n]*\) => (\S+)", ldconfig, re.MULTILINE))
for index in range(count.value):
    namespace = names[index]
    path = Path(gi.gi_repository_get_typelib_path(repo, namespace).decode())
    copy(path, LIB / "girepository-1.0" / path.name)
    size = ctypes.c_size_t()
    libraries = gi.gi_repository_get_shared_libraries(repo, namespace, ctypes.byref(size))
    for item in range(size.value):
        name = libraries[item].decode()
        if name not in resolved:
            raise SystemExit(f"Missing typelib library: {name}")
        copy(Path(resolved[name]), LIB / name)

# OpenCL's ICD loader is portable. The vendor ICD/compiler and render device stay on the host.
# copy() records its package, hash, licence and ELF closure; system_sources.py collects matching source.
if "libOpenCL.so.1" not in resolved:
    raise SystemExit("Missing OpenCL ICD loader: install ocl-icd-libopencl1")
copy(Path(resolved["libOpenCL.so.1"]), LIB / "libOpenCL.so.1")

# The optional CinemaDNG adapter is built after this closure is collected. Include its shared
# runtime now, with the same package/source/licence records as the other system libraries.
if "libfuse3.so.4" in resolved:
    copy(Path(resolved["libfuse3.so.4"]), LIB / "libfuse3.so.4")

# WebKit launches these programs itself, outside Python's import and ELF dependencies.
tree(SYSTEM / "webkit2gtk-4.1", LIB / "webkit2gtk-4.1")
tree(SYSTEM / "gio/modules", LIB / "gio/modules")
tree(SYSTEM / "gdk-pixbuf-2.0/2.10.0/loaders", LIB / "gdk-pixbuf-2.0/loaders")
copy(SYSTEM / "gdk-pixbuf-2.0/gdk-pixbuf-query-loaders", APPDIR / "usr/bin/gdk-pixbuf-query-loaders")
# WebKit creates a media backend even for <audio preload="none">. Include its app sinks,
# playback, HTTP source and PCM WAV path, plus a real desktop audio sink.
for plugin in (
    "coreelements",
    "app",
    "autodetect",
    "playback",
    "typefindfunctions",
    "wavparse",
    "audioconvert",
    "audioresample",
    "volume",
    "pulseaudio",
    "soup",
    "debugutilsbad",
    "audiofx",
    "subenc",
):
    name = f"libgst{plugin}.so"
    copy(SYSTEM / "gstreamer-1.0" / name, LIB / "gstreamer-1.0" / name)
copy(Path("/usr/bin/gst-inspect-1.0"), APPDIR / "usr/bin/gst-inspect-1.0")
scanner = SYSTEM / "gstreamer1.0/gstreamer-1.0/gst-plugin-scanner"
copy(scanner, APPDIR / "usr/libexec/gstreamer-1.0/gst-plugin-scanner")
schemas = APPDIR / "usr/share/glib-2.0/schemas"
schemas.mkdir(parents=True, exist_ok=True)
for path in Path("/usr/share/glib-2.0/schemas").glob("org.gtk.*.xml"):
    copy(path, schemas / path.name)
subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
copy(Path("/usr/share/icons/hicolor/index.theme"), APPDIR / "usr/share/icons/hicolor/index.theme")
mime = APPDIR / "usr/share/mime"
copy(Path("/usr/share/mime/packages/freedesktop.org.xml"), mime / "packages/freedesktop.org.xml")
subprocess.run(["update-mime-database", str(mime)], check=True)
for name in ("icons/Adwaita",):
    tree(Path("/usr/share") / name, APPDIR / "usr/share" / name)

# Include Python extension dependencies as well as FFmpeg's two programs.
queue.extend(path for path in (APPDIR / "usr/python").rglob("*") if path.is_file() and ".so" in path.name)
queue.extend((APPDIR / "usr/bin" / name) for name in ("ffmpeg", "ffprobe"))
seen: set[Path] = set()
while queue:
    path = queue.pop()
    if path in seen:
        continue
    seen.add(path)
    output = subprocess.check_output(
        ["ldd", str(path)],
        text=True,
        env=dict(os.environ, LD_LIBRARY_PATH=str(APPDIR / "usr/python/lib")),
    )
    if "not found" in output:
        raise SystemExit(f"Unresolved dependency of {path}:\n{output}")
    for name, location in re.findall(r"^\s*(\S+) => (/\S+)", output, re.MULTILINE):
        if not GLIBC.match(name):
            source = Path(location)
            # Wheel-local libraries keep their layout and RPATH; do not flatten them.
            if not source.is_relative_to(APPDIR):
                copy(source, LIB / name)

LICENCES.mkdir(exist_ok=True)
shutil.copy2(ROOT / "LICENSE", LICENCES / "mcrawsome-Apache-2.0.txt")
for old in LICENCES.glob("system-*.txt"):
    old.unlink()
for package in sorted(packages):
    name = package.split(":")[0]
    copyright_file = Path("/usr/share/doc") / name / "copyright"
    if not copyright_file.is_file():
        raise SystemExit(f"Missing copyright file for {package}")
    shutil.copy2(copyright_file, LICENCES / f"system-{name}.txt")
    manifest[package] = run("dpkg-query", "-W", "-f=${Version}", package)
for path in Path("/usr/share/common-licenses").iterdir():
    if path.is_file():
        shutil.copy2(path, LICENCES / f"common-{path.name}.txt")
shutil.copy2(APPDIR / "usr/python/lib/python3.12/LICENSE.txt", LICENCES / "Python-3.12.txt")
with tarfile.open(CACHE / "downloads/python-build-standalone-source.tar.gz") as archive:
    for member in archive.getmembers():
        name = Path(member.name).name
        if member.isfile() and name.startswith("LICENSE."):
            stream = archive.extractfile(member)
            assert stream is not None
            with stream:
                (LICENCES / f"Python-runtime-{name}").write_bytes(stream.read())

for dist in importlib.metadata.distributions():
    name = dist.metadata["Name"]
    if name == "mcrawsome":
        continue
    texts = []
    for file in dist.files or []:
        if any(
            part.lower().startswith(("license", "licence", "copying", "copyright", "notice"))
            for part in file.parts
        ):
            source = Path(dist.locate_file(file))
            if source.is_file():
                texts.append(f"{file}\n\n{source.read_text(errors='replace')}")
    if not texts:
        # A few wheels omit their licence. Recover it from the exact version's verified sdist.
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{dist.version}/json") as response:
            metadata = json.load(response)
        archive = next(item for item in metadata["urls"] if item["packagetype"] == "sdist")
        downloaded = CACHE / "downloads" / archive["filename"]
        if not downloaded.exists():
            urllib.request.urlretrieve(archive["url"], downloaded)
        if hashlib.sha256(downloaded.read_bytes()).hexdigest() != archive["digests"]["sha256"]:
            raise SystemExit(f"Checksum mismatch for {downloaded}")
        with tarfile.open(downloaded) as source_archive:
            for member in source_archive.getmembers():
                if member.isfile() and Path(member.name).name.lower().startswith(
                    ("license", "licence", "copying", "copyright", "notice")
                ):
                    stream = source_archive.extractfile(member)
                    assert stream is not None
                    with stream:
                        texts.append(f"{member.name}\n\n{stream.read().decode(errors='replace')}")
    if not texts:
        overrides = tomllib.loads((ROOT / "packaging/appimage/python-licence-overrides.toml").read_text())
        for entry in overrides["licences"]:
            if name.replace("-", "_") != entry["name"] or dist.version != entry["version"]:
                continue
            module = Path(dist.locate_file(entry["module"]))
            if hashlib.sha256(module.read_bytes()).hexdigest() != entry["module_sha256"]:
                raise SystemExit(f"The licence override no longer matches {name}'s source")
            with urllib.request.urlopen(entry["url"]) as response:
                licence_text = response.read()
            if hashlib.sha256(licence_text).hexdigest() != entry["sha256"]:
                raise SystemExit(f"The licence override checksum changed for {name}")
            texts.append(
                "The distribution metadata says MIT; the shipped module's header refers to BSD.\n"
                "The identical upstream source accompanies this BSD licence.\n"
                + entry["url"]
                + "\n\n"
                + licence_text.decode()
            )
    if not texts:
        raise SystemExit(f"No licence text found for {name} {dist.version}")
    (LICENCES / f"Python-{name}.txt").write_text(f"{name} {dist.version}\n\n" + "\n\n".join(texts))
    manifest[f"Python-{name}"] = dist.version
shutil.copy2(ROOT / "web/node_modules/svelte/LICENSE.md", LICENCES / "Svelte-MIT.txt")
record = APPDIR / "usr/share/mcrawsome/runtime-manifest.json"
record.parent.mkdir(parents=True, exist_ok=True)
record.write_text(json.dumps(manifest, indent=2) + "\n")
# Keep the project's punctuation rule without changing the words of upstream notices.
for notice in LICENCES.iterdir():
    if notice.is_file():
        text = notice.read_text()
        normalised = text.replace(chr(0x2014), "--")
        if normalised != text:
            notice.write_text(normalised)
print(f"Bundled {len(copied)} files from {len(packages)} system packages")
