"""Check the relocated ELF closure and import the window backend without opening a display.

Every file must also run on glibc GLIBC_FLOOR, the oldest the image supports (Ubuntu 24.04, Linux Mint 22,
Fedora 40): an update on the build host that needs a newer one fails here rather than on users' systems.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

appdir = Path(sys.argv[1]).resolve()
env = dict(
    os.environ,
    LD_LIBRARY_PATH=f"{appdir}/usr/lib:{appdir}/usr/python/lib",
    GI_TYPELIB_PATH=str(appdir / "usr/lib/girepository-1.0"),
    PYTHONNOUSERSITE="1",
    GIO_MODULE_DIR=str(appdir / "usr/lib/gio/modules"),
    GIO_EXTRA_MODULES="",
)
env.pop("PYTHONPATH", None)
env.pop("PYTHONHOME", None)
glibc = re.compile(r"^(ld-linux|lib(c|m|pthread|dl|rt|resolv|util|nss_[^.]+)\.so[.\d]*$)")
GLIBC_FLOOR = (2, 39)
newest: tuple[tuple[int, int], Path] = ((0, 0), appdir)
checked = 0
for path in appdir.rglob("*"):
    if not path.is_file():
        continue
    with path.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            continue
    output = subprocess.run(["ldd", str(path)], capture_output=True, text=True, env=env)
    if "not found" in output.stdout:
        raise SystemExit(f"Unresolved dependency: {path}\n{output.stdout}")
    for name, location in re.findall(r"^\s*(\S+) => (/\S+)", output.stdout, re.MULTILINE):
        if not Path(location).is_relative_to(appdir) and not glibc.match(name):
            raise SystemExit(f"Unbundled dependency: {path}: {name} => {location}")
    symbols = subprocess.run(["objdump", "-T", str(path)], capture_output=True, text=True).stdout
    for major, minor in re.findall(r"GLIBC_(\d+)\.(\d+)", symbols):
        newest = max(newest, ((int(major), int(minor)), path))
    checked += 1
if newest[0] > GLIBC_FLOOR:
    raise SystemExit(f"{newest[1]} needs glibc {newest[0][0]}.{newest[0][1]}, newer than {GLIBC_FLOOR}")
if any((appdir / "usr/python").rglob("_fuse*.so")):
    subprocess.run(
        [
            str(appdir / "usr/python/bin/python3"),
            "-c",
            "import mcrawsome._fuse; print('FUSE adapter imports')",
        ],
        check=True,
        env=env,
    )
subprocess.run(
    [
        str(appdir / "usr/python/bin/python3"),
        "-c",
        "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('WebKit2', '4.1'); "
        "from gi.repository import Gtk, WebKit2; import webview, mcrawsome._raw; "
        "print('GTK, WebKitGTK, pywebview and the native engine import successfully')",
    ],
    check=True,
    env=env,
)
for name in ("WebKitNetworkProcess", "WebKitWebProcess"):
    path = appdir / "usr/lib/webkit2gtk-4.1" / name
    if not os.access(path, os.X_OK):
        raise SystemExit(f"Missing executable WebKit helper: {path}")
print(
    f"Checked {checked} ELF files; only the host glibc family remains outside the AppDir, "
    f"and they need glibc {newest[0][0]}.{newest[0][1]} at most"
)
subprocess.run(
    [str(appdir / "usr/python/bin/python3"), str(Path(__file__).with_name("audit_python_runtime.py"))],
    check=True,
    env=env,
)

# A host plugin or a warm registry must not hide an incomplete media bundle.
with tempfile.TemporaryDirectory(prefix="mcrawsome-gst-", dir=appdir.parent) as registry:
    media_env = dict(
        env,
        GST_PLUGIN_SYSTEM_PATH_1_0=str(appdir / "usr/lib/gstreamer-1.0"),
        GST_PLUGIN_PATH_1_0="",
        GST_PLUGIN_PATH="",
        GST_PLUGIN_SCANNER=str(appdir / "usr/libexec/gstreamer-1.0/gst-plugin-scanner"),
        GST_REGISTRY_1_0=str(Path(registry) / "registry.bin"),
    )
    for element in (
        "appsink",
        "appsrc",
        "autoaudiosink",
        "playbin",
        "decodebin",
        "wavparse",
        "audioconvert",
        "audioresample",
        "volume",
        "pulsesink",
        "souphttpsrc",
        "fakevideosink",
        "scaletempo",
        "webvttenc",
    ):
        result = subprocess.run(
            [str(appdir / "usr/bin/gst-inspect-1.0"), element],
            capture_output=True,
            text=True,
            env=media_env,
        )
        if result.returncode:
            raise SystemExit(f"Missing GStreamer element: {element}\n{result.stderr}")
print("Checked WebKit GStreamer elements with only bundled plugins")
