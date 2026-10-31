# Debian package

MCRAWsome ships as a `.deb` for Debian 13 (trixie). It uses Debian's own Python 3.13, FFmpeg,
WebKitGTK, GTK, GStreamer and Python packages, so Debian's security updates reach them, and ships
only what Debian lacks.

## Build

Install the build prerequisites once (the builder lists any that are missing):

```sh
sudo apt install python3-dev python3-venv python3-packaging libfuse3-dev cmake g++ \
  python3-numpy python3-scipy python3-opencv python3-pydantic python3-click python3-rich \
  python3-tomli-w python3-websockets python3-webview python3-gi python3-gi-cairo \
  ffmpeg fuse3 xdg-utils gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-gstreamer-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

`uv` and Node 22 or later are also needed, to build the wheel and the interface. Then:

```sh
/usr/bin/python3 -I packaging/deb/build_deb.py /home/thom/Applications
```

Increment `DEBIAN_REVISION` for each new build. The builder refuses to overwrite an existing package.
Rebuilt packages are saved in `/home/thom/Applications`.

The builder never installs system packages. It:

1. builds the interface and a wheel for `/usr/bin/python3`, including the `_raw` and FUSE 3
   modules;
2. asks pip, in a virtual environment that sees Debian's packages, what Debian does not already
   satisfy, and vendors exactly that, at the versions in `uv.lock`, into
   `/usr/lib/mcrawsome/vendor` (as of October 2026: fastapi, starlette, uvicorn, watchfiles,
   openexr, opencolorio, pywebview and their missing dependencies);
3. installs MCRAWsome into `/usr/lib/python3/dist-packages`, with a `VENDOR` file that puts the
   vendored folder first on `sys.path` for MCRAWsome's processes only;
4. writes `Depends` from what was resolved: the Debian packages providing every Python requirement
   (transitively), the libraries the compiled modules load, and the programs used at run time,
   each at least at the version installed on the build host;
5. adds the menu entry, icon, AppStream metadata, default Nautilus actions and `.mcraw` MIME type,
   depends on `python3-nautilus`, and byte-compiles on
   installation as `dh_python3` would.

The compiled modules are built for one Python version, so the package depends on
`python3 (>= 3.13), python3 (<< 3.14)`. Build again on each Debian release.

Debian's packages are often older than `uv.lock`'s (for example numpy 2.2 rather than 2.5, OpenCV
4.10 rather than 5.0). Run the tests against them before a release: create a virtual environment
with `--system-site-packages` from `/usr/bin/python3`, install the vendored packages and
MCRAWsome's development dependencies into it, and run `pytest`. If a Debian package proves too old,
raise MCRAWsome's minimum in `pyproject.toml`; the builder then vendors it.

## Nautilus menu and file associations

The package installs the GNOME Files provider globally and depends on `python3-nautilus` so the
MCRAWsome submenu works by default. Restart Files after installing or updating the package.
It offers Send to Resolve (Media Bin) and Send to Resolve (Timeline) for selected `.mcraw` files,
without opening a window. Settings > General > File manager disables or re-enables the menu for
this account. The saved disabled preference survives upgrades and reinstalls. Disabling does not
remove the shared loader. The provider checks the preference again when an action is activated.

The package installs `/usr/share/thumbnailers/mcrawsome.thumbnailer`, so file managers show each
`.mcraw` recording's first frame (`mcrawsome/thumbnailer.py`). GNOME runs a thumbnailer under
bubblewrap with only `/usr`, a few files from `/etc` and the recording itself, read-only, no network and
a seccomp filter, so it reads no settings, uses no GPU and writes only the PNG it is asked for.

Opening `.mcraw` files uses the desktop launcher, which accepts multiple files. Users can change
the association in their file manager. The package does not overwrite personal MIME defaults.

## Desktop runtime compatibility

The application requires pywebview 6.2.1 or later for `before_show`, `FileDialog`, native window
access and the GTK JavaScript integration. Debian 13 supplies 5.0.5, so pywebview is vendored at the
version pinned in `uv.lock`. GTK, WebKitGTK, GI and GStreamer still come from Debian. The tiny
pure-Python `proxy-tools` dependency ships only a source archive; the builder permits that one
source distribution while retaining binary-only resolution for other packages.

## GNOME Software and local packages

The package includes standard AppStream metadata with the application ID, desktop launcher, icon,
Apache-2.0 licence, description, OARS content rating and release notes. The Debian control file also
includes the project homepage.

GNOME Software 48.3's PackageKit local-file path reads the Debian control description, homepage,
version, installed size and desktop filename. It does not extract the embedded AppStream file or
icon. PackageKit 1.3.1's APT backend reports an unknown licence for a local deb. Consequently the
first-open local-file page can still show a generic icon, unknown licence, no rating, no screenshots
and no release notes. Download size is explicitly zero because the file is already local. The
third-party and no-repository notices reflect how the package is distributed. Adding metadata
cannot honestly remove those notices or create automatic updates.

A repository AppStream catalogue is the route to a complete Software listing, including publicly
hosted screenshots. No repository or screenshot URLs have been invented for this package.
See [GNOME's metadata guide](https://help.gnome.org/gnome-software/software-metadata.html),
[GNOME Software's local-file reader](https://github.com/GNOME/gnome-software/blob/48.3/plugins/packagekit/gs-plugin-packagekit.c)
and [PackageKit's APT backend](https://github.com/PackageKit/PackageKit/blob/v1.3.1/backends/apt/apt-job.cpp).


## Engine restart during upgrades

Starting with revision10, new mounts run in independent user services from private, content-addressed copies of the
application and its bundled vendor dependencies under the user's state directory `mount-runtimes/`.
Upgrading restarts the engine and closes its GUI while those services and their open file handles
remain intact. The replacement engine discovers the existing mount records, and installation checks
that the same service PIDs and mount identities are available. New mounts use the new runtime;
existing mounts keep their original code and settings until unmounted. Normal unmount or the next
mount preparation removes unused runtime copies. Debian's system Python and libraries remain shared;
this supports MCRAWsome upgrades within the same Debian runtime, not a live OS/Python ABI migration.

The first upgrade from revision9 or earlier must release old mounts once: these lack pinned runtimes or independent service groups.
Close Resolve and retry installation; normal unmount is used, never force or lazy unmount. Following
upgrades can preserve mounts while Resolve stays open. Uninstall continues to require safe normal
unmounts and stops all engines and mount services; busy files prevent removal. User settings,
recordings and mount records are retained. No application data or original media is deleted.


Each engine, mount and Resolve file handler has its own transient systemd user service. Detaching a
POSIX session alone does not escape PackageKit's service cgroup: PackageKit stopping could otherwise
terminate the engine, filesystem services and Resolve together. Desktop handlers use ExitType=cgroup
so an xdg-open launcher exiting does not kill a forked Resolve process. The engine's health PID is
checked against the service's real MainPID, not the systemd-run wait process. Mount preservation
requires a verified independent mount service group as well as its pinned runtime. Package restart
uses the desktop user's manager via runuser; it never leaves the engine in the installer service.
