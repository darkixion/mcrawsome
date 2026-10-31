"""Running inside MCRAWsome's AppImage, and starting the host's own programs from it. The .deb runs on
the system's own Python and libraries, so none of this applies to it."""

from __future__ import annotations

import os
from collections.abc import Mapping

# AppRun saves these before pointing them into the bundle.
RESTORED = ("PATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "XDG_DATA_DIRS", "GIO_EXTRA_MODULES")
# Set by the AppImage runtime or AppRun for the bundle only; a host program must not see them.
BUNDLE_ONLY = (
    "APPIMAGE",
    "APPDIR",
    "ARGV0",
    "OWD",
    "MCRAWSOME_APPIMAGE",
    "MCRAWSOME_LAUNCHER",
    "MCRAWSOME_FFMPEG",
    "MCRAWSOME_WEBKIT_HELPERS",
    "GI_TYPELIB_PATH",
    "GIO_MODULE_DIR",
    "GDK_PIXBUF_MODULE_FILE",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
    "GST_PLUGIN_SCANNER",
    "GST_PLUGIN_PATH_1_0",
    "GST_PLUGIN_PATH",
    "WEBKIT_EXEC_PATH",
    "WEBKIT_INJECTED_BUNDLE_PATH",
    "PYTHONNOUSERSITE",
    "PYTHONSAFEPATH",
)


def running_image() -> str | None:
    """mcrawsome's own AppImage, as AppRun records it. APPIMAGE alone is inherited from any AppImage
    (a terminal or an editor), so it does not count."""
    return os.environ.get("MCRAWSOME_APPIMAGE") or None


def launcher() -> str | None:
    """The program that starts MCRAWsome with its bundled environment: the AppImage file, or AppRun where
    the image is extracted. None when run from a Python environment (or the .deb)."""
    return os.environ.get("MCRAWSOME_LAUNCHER") or running_image()


def host_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a host program (xdg-open, a browser, the launcher itself): inside the
    bundle, the host's own search paths and none of the bundle's libraries or modules."""
    environment = dict(os.environ if environ is None else environ)
    if not environment.get("MCRAWSOME_APPIMAGE") and not environment.get("MCRAWSOME_LAUNCHER"):
        return environment
    for name in RESTORED:
        original = environment.pop(f"MCRAWSOME_ORIGINAL_{name}", "")
        if original:
            environment[name] = original
        else:
            environment.pop(name, None)
    for name in BUNDLE_ONLY:
        environment.pop(name, None)
    return environment
