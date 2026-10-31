# AppImage

For Linux systems without the Debian package: one file that carries everything MCRAWsome needs and runs
where glibc is 2.39 or later (Ubuntu 24.04, Linux Mint 22, Fedora 40, Arch and later). Debian users
should install the `.deb` instead, which uses Debian's own packages and gets their security updates.
Only the `.deb` gives `.mcraw` recordings thumbnails in file managers: GNOME runs thumbnailers in a
sandbox that sees `/usr` alone, never an AppImage in a home folder.

## What it carries

- Python 3.12 (python-build-standalone) with the packages pinned in `uv.lock`, MCRAWsome's native
  modules and the built interface.
- A private FFmpeg 8.1 that only decodes (`build_ffmpeg.sh`): Direct Log's H.265, ProRes-compatible and
  AV1 video (dav1d), their audio and telemetry, with VAAPI or NVIDIA's NVDEC where the host has them.
  It is LGPL and statically linked, so no FFmpeg on the host is used. NVDEC is built from NVIDIA's MIT
  headers alone; the driver's own libraries are loaded at run time.
- GTK 3, WebKitGTK 4.1, GStreamer's playback elements and their libraries, copied from the Debian 13
  build host with each package's copyright notice (`bundle.py`). WebKit's helper programs are started
  from the image by a small preload shim (`webkit-launcher.c`), as Debian's WebKit ignores
  `WEBKIT_EXEC_PATH`.

From the host it uses glibc, the graphics drivers (OpenGL, OpenCL, VAAPI, NVIDIA's CUDA and NVDEC) and
FUSE 3 for mounting.
Engines and mount services are started through the image file, so each keeps its own copy of the
program after the window closes. Host programs (`xdg-open`, the browser, Resolve) get the host's own
environment back. On first run the window offers to add the image to the application menu and to
register it for `.mcraw` recordings; `mcrawsome.AppImage desktop-entry [--remove]` does the same.

## Build

On Debian 13 (trixie), x86_64, with the `.deb`'s build prerequisites (`packaging/deb/README.md`) and:

```sh
sudo apt install libcairo2-dev libgirepository-2.0-dev libva-dev libdrm-dev meson ninja-build nasm \
  desktop-file-utils binutils
```

Then:

```sh
packaging/appimage/build_appimage.py ~/Applications
```

It writes `mcrawsome-VERSION-REVISION-x86_64.AppImage` and its `.sha256`, sharing the `.deb`'s
`DEBIAN_REVISION`, and refuses to overwrite an existing image. The first build also builds FFmpeg
(about 10 minutes, at nice 19 and at most four jobs) into `~/.cache/mcrawsome-ffmpeg/decode-prefix`.
Downloads, the AppDir and work files stay in `~/.cache/mcrawsome-appimage`. Python, appimagetool and
the AppImage runtime are fetched once from pinned releases and checked by SHA-256.

The build fails rather than ship an image that would not work:

- `audit_ffmpeg.py`: the decoders, filters and outputs MCRAWsome uses are present, FFmpeg is LGPL, and no
  FFmpeg library comes from the host.
- `audit_appdir.py`: every bundled program and library resolves inside the image (only glibc's own
  family outside it), needs glibc 2.39 at most, GTK, WebKitGTK, pywebview and the native modules
  import, and WebKit's media elements load from the bundled GStreamer alone.
- `audit_python_runtime.py`: every OpenCV and SciPy name MCRAWsome uses resolves in the pruned runtime.

`prune_runtime.py` removes build headers, tests and the SciPy subpackages MCRAWsome never loads
(measured, and checked by the audit above).

## Release sources

A public release must offer the source of the LGPL components it carries. Add `--sources` to also write
`…-sources.tar.xz` (FFmpeg and dav1d with these build materials) and `…-system-sources.tar` (the Debian
source of every bundled system package, about 800 MB; enable `deb-src` lines in apt first), and publish
them beside the image.

## Checks

On this machine, `window_smoke.py IMAGE --clip CLIP --output DIR` opens the real window for a few seconds
with its own state, settings and menu data, and fails on a GLib or WebKit critical message or an unclean
close. In containers of other distributions, run the image with `APPIMAGE_EXTRACT_AND_RUN=1`
(containers have no FUSE): its `doctor`, an engine scanning a clip and a RAW thumbnail should all work.
