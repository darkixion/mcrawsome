"""The AppImage: its launcher, WebKit helper shim, audits, window smoke test and build order."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = (ROOT / "packaging/appimage/build_appimage.py").read_text()


def test_webkit_launcher_relocates_helpers_only(tmp_path: Path) -> None:
    if not shutil.which("cc") or not shutil.which("pkg-config"):
        pytest.skip("needs a C compiler and pkg-config")
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gio-2.0"], capture_output=True, text=True)
    if flags.returncode:
        pytest.skip("needs the GLib development files")
    shim = tmp_path / "webkit-launcher.so"
    subprocess.run(
        ["cc", "-shared", "-fPIC", str(ROOT / "packaging/appimage/webkit-launcher.c"),
         "-o", str(shim), *flags.stdout.split(), "-ldl"], check=True,
    )  # fmt: skip
    driver = tmp_path / "driver.c"
    driver.write_text("""#include <gio/gio.h>
#include <stdio.h>
int main(int argc, char **argv) {
    GSubprocessLauncher *launcher = g_subprocess_launcher_new(G_SUBPROCESS_FLAGS_STDOUT_PIPE);
    GSubprocess *child = g_subprocess_launcher_spawnv(launcher, (const char *const *)(argv + 1), NULL);
    if (!child) return 1;
    char *output = NULL;
    g_subprocess_communicate_utf8(child, NULL, NULL, &output, NULL, NULL);
    fputs(output, stdout);
    g_free(output);
    g_object_unref(child);
    g_object_unref(launcher);
    return 0;
}
""")
    executable = tmp_path / "driver"
    subprocess.run(["cc", str(driver), "-o", str(executable), *flags.stdout.split()], check=True)
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    echo = shutil.which("echo")
    assert echo is not None
    shutil.copy2(echo, helpers / "WebKitNetworkProcess")
    env = dict(os.environ, LD_PRELOAD=str(shim), MCRAWSOME_WEBKIT_HELPERS=str(helpers))
    for program in ("/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1/WebKitNetworkProcess", echo):
        result = subprocess.run([str(executable), program, "smoke"], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "smoke\n"


def _appdir(tmp_path: Path, python: str) -> Path:
    """A stand-in AppDir around the real AppRun: its Python is `python`, a shell script."""
    appdir = tmp_path / "mcrawsome.AppDir"
    for folder in ("usr/bin", "usr/python/bin"):
        (appdir / folder).mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/appimage/AppRun", appdir / "AppRun")
    for path, text in (("usr/bin/gdk-pixbuf-query-loaders", ":\n"), ("usr/python/bin/python3", python)):
        (appdir / path).write_text("#!/bin/sh\n" + text)
        (appdir / path).chmod(0o755)
    return appdir


def test_the_working_folder_cannot_replace_bundled_modules(tmp_path: Path) -> None:
    appdir = _appdir(tmp_path, f'exec "{sys.executable}" "$@"\n')
    work = tmp_path / "work"
    work.mkdir()
    (work / "json.py").write_text("raise SystemExit('the working folder shadowed json')\n")
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path))
    env.pop("APPDIR", None)
    result = subprocess.run(
        [str(appdir / "AppRun"), "--version"], cwd=work, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "version" in result.stdout


def _environment(tmp_path: Path, **extra: str) -> dict[str, str]:
    dump = tmp_path / "environment"
    appdir = _appdir(tmp_path, f'env > "{dump}"\n')
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path), **extra)
    for name in ("APPDIR", "APPIMAGE", "MCRAWSOME_APPIMAGE", "MCRAWSOME_LAUNCHER"):
        if name not in extra:
            env.pop(name, None)
    extra_appdir = env.get("APPDIR")
    if extra_appdir == "SELF":
        env["APPDIR"] = str(appdir)
    subprocess.run([str(appdir / "AppRun"), "--version"], cwd=tmp_path, env=env, check=True)
    return dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)


def test_apprun_names_its_own_image(tmp_path: Path) -> None:
    image = str(tmp_path / "mcrawsome.AppImage")
    environment = _environment(tmp_path, APPIMAGE=image, APPDIR="SELF")
    assert environment["MCRAWSOME_APPIMAGE"] == image
    assert environment["MCRAWSOME_LAUNCHER"] == image
    assert environment["PYTHONSAFEPATH"] == "1"


def test_apprun_ignores_another_images_name(tmp_path: Path) -> None:
    # Started from a terminal that is itself an AppImage: APPIMAGE and APPDIR are the terminal's.
    environment = _environment(tmp_path, APPIMAGE="/opt/Terminal.AppImage", APPDIR="/tmp/.mount_Term")
    assert "MCRAWSOME_APPIMAGE" not in environment
    assert environment["APPDIR"] == str(tmp_path / "mcrawsome.AppDir")


def test_an_extracted_image_launches_through_its_apprun(tmp_path: Path) -> None:
    # Run from an extracted AppDir, engines and services start through its AppRun again.
    environment = _environment(tmp_path, MCRAWSOME_LAUNCHER="/stale/launcher")
    assert "MCRAWSOME_APPIMAGE" not in environment
    assert environment["MCRAWSOME_LAUNCHER"] == str(tmp_path / "mcrawsome.AppDir/AppRun")


@pytest.mark.parametrize(
    "missing", [None, "appsink", "autoaudiosink", "wavparse", "fakevideosink", "scaletempo", "webvttenc"]
)
def test_bundle_audit_checks_media_elements_without_host_plugins(tmp_path: Path, missing: str | None) -> None:
    appdir = tmp_path / "AppDir"
    for name in (
        "usr/python/bin/python3",
        "usr/lib/webkit2gtk-4.1/WebKitNetworkProcess",
        "usr/lib/webkit2gtk-4.1/WebKitWebProcess",
        "usr/bin/gst-inspect-1.0",
    ):
        path = appdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    inspector = appdir / "usr/bin/gst-inspect-1.0"
    inspector.write_text(
        '#!/bin/sh\n[ "$GST_PLUGIN_SYSTEM_PATH_1_0" = "$EXPECT_PLUGINS" ] || exit 2\n'
        '[ -z "$GST_PLUGIN_PATH_1_0" ] || exit 3\n'
        + (f'[ "$1" != "{missing}" ]\n' if missing else "exit 0\n")
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "packaging/appimage/audit_appdir.py"), str(appdir)],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            EXPECT_PLUGINS=str(appdir / "usr/lib/gstreamer-1.0"),
            GST_PLUGIN_PATH_1_0="/host/plugins",
        ),
    )
    assert (result.returncode == 0) is (missing is None), result.stdout + result.stderr
    if missing:
        assert f"Missing GStreamer element: {missing}" in result.stdout + result.stderr


def test_apprun_excludes_extra_host_gio_modules(tmp_path: Path) -> None:
    environment = _environment(tmp_path, GIO_EXTRA_MODULES="/host/incompatible/gio/modules")
    assert "GIO_EXTRA_MODULES" not in environment
    assert environment["MCRAWSOME_ORIGINAL_GIO_EXTRA_MODULES"] == "/host/incompatible/gio/modules"


def test_window_smoke_detects_glib_and_libsoup_criticals() -> None:
    import runpy

    smoke = runpy.run_path(str(ROOT / "packaging/appimage/window_smoke.py"))
    lines = [
        "(process:12): GLib-GObject-CRITICAL **: g_object_ref: assertion failed",
        "(process:12): libsoup-CRITICAL **: soup_session_feature_request_queued: assertion failed",
    ]
    assert smoke["critical_lines"]("ready\n" + "\n".join(lines)) == lines
    assert smoke["critical_lines"]("GTK and WebKit started\n") == []


def test_window_smoke_keeps_maps_observed_before_process_exit() -> None:
    import runpy

    smoke = runpy.run_path(str(ROOT / "packaging/appimage/window_smoke.py"))
    first = {123: ("WebKitNetworkProcess", ["/bundle/libsoup-3.0.so", "/host/gio/modules/proxy.so"])}
    final = {123: ("WebKitNetworkProcess", []), 456: ("engine", ["/bundle/libglib.so"])}
    assert smoke["merge_processes"](first, final) == {
        123: ("WebKitNetworkProcess", ["/bundle/libsoup-3.0.so", "/host/gio/modules/proxy.so"]),
        456: ("engine", ["/bundle/libglib.so"]),
    }


@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("webprocess_alive", [False, True])
def test_window_smoke_checks_normal_close_and_its_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, critical: bool, webprocess_alive: bool
) -> None:
    import io
    import json
    import runpy
    from typing import Any

    smoke = runpy.run_path(str(ROOT / "packaging/appimage/window_smoke.py"))
    namespace = smoke["main"].__globals__
    closed = False

    class Process:
        pid = 123

        def poll(self) -> int | None:
            return 0 if closed else None

        def wait(self, timeout: float) -> int:
            return 0

    def close(pids: set[int]) -> bool:
        nonlocal closed
        assert pids == {123, 456}
        closed = True
        if critical:
            with (tmp_path / "window.log").open("a") as log:
                log.write("libsoup-CRITICAL: soup_session_feature_detach\n")
        return True

    observations = 0

    def processes(roots: set[int]) -> dict[int, tuple[str, list[str]]]:
        nonlocal observations
        observations += 1
        name = "WebKitWebProcess" if webprocess_alive or observations == 1 else "engine"
        return {123: ("window", []), 456: (name, [])}

    original = Path.read_bytes

    def read(path: Path) -> bytes:
        if str(path) == "/proc/456/environ":
            return f"MCRAWSOME_STATE_DIR={tmp_path / 'state'}".encode()
        return original(path)

    def urlopen(*args: Any, **kwargs: Any) -> io.BytesIO:
        return io.BytesIO(b'{"pid":456}')

    monkeypatch.setattr(
        sys,
        "argv",
        ["smoke", "--source", "--output", str(tmp_path), "--duration", "0.01", "--timeout", "0.15"],
    )
    monkeypatch.setitem(namespace, "close_window", close)
    monkeypatch.setitem(namespace, "processes", processes)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(namespace["urllib"].request, "urlopen", urlopen)
    monkeypatch.setattr(os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(Path, "read_bytes", read)
    assert smoke["main"]() == (0 if webprocess_alive and not critical else 1)
    result = json.loads((tmp_path / "result.json").read_text())
    assert closed == webprocess_alive
    assert result["window_started"] == webprocess_alive
    assert result["window_closed"] == webprocess_alive
    assert bool(result["criticals"]) == (critical and webprocess_alive)


def test_the_interface_is_built_before_bundling() -> None:
    # bundle.py copies web/node_modules/svelte's licence, which only npm ci (build_web.py) provides.
    assert 0 < BUILD.index("scripts/build_web.py") < BUILD.index('"bundle.py"')


def test_mcrawsome_is_installed_before_bundling_and_audited_before_packing() -> None:
    # The bundler writes the notices into the installed package and follows its native modules' libraries.
    install = BUILD.index('"--no-deps", "--reinstall", wheel')
    assert install < BUILD.index('"bundle.py"') < BUILD.index('"prune_runtime.py"')
    assert (
        BUILD.index('"prune_runtime.py"') < BUILD.index('"audit_appdir.py"') < BUILD.index("--runtime-file")
    )


def test_the_wheel_is_built_for_the_bundled_python_alone() -> None:
    build = next(line for line in BUILD.splitlines() if '"uv", "build"' in line)
    assert '"--python", python' in build
    assert "shutil.rmtree(wheels, ignore_errors=True)" in BUILD


def test_the_image_shares_the_debs_revision_and_never_overwrites() -> None:
    import runpy

    builder = runpy.run_path(str(ROOT / "packaging/appimage/build_appimage.py"))
    deb = runpy.run_path(str(ROOT / "packaging/deb/build_deb.py"))
    assert builder["revision"]() == deb["DEBIAN_REVISION"]
    assert "Refusing to overwrite" in BUILD


def test_nothing_is_written_to_the_checkout() -> None:
    bundle = (ROOT / "packaging/appimage/bundle.py").read_text()
    assert 'ROOT / "packaging/LICENCES"' not in bundle
    assert "site-packages/mcrawsome/packaging/LICENCES" in bundle
    ffmpeg = (ROOT / "packaging/appimage/build_ffmpeg.sh").read_text()
    assert "../LICENCES" not in ffmpeg
    assert '"$prefix/share/mcrawsome"' in ffmpeg


def test_ffmpeg_is_decode_only_and_lgpl() -> None:
    options = (ROOT / "packaging/appimage/ffmpeg-configure.txt").read_text().split()
    assert "--disable-gpl" in options
    assert "--disable-nonfree" in options
    assert [option for option in options if option.startswith("--enable-lib")] == [
        "--enable-libdav1d",
        "--enable-libdrm",
    ]


def test_ffmpeg_decodes_on_nvidia_gpus_with_headers_alone() -> None:
    """NVDEC needs only NVIDIA's MIT headers: FFmpeg loads the driver's libraries at run time, so no CUDA
    compiler, NPP or encoder is built in."""
    options = (ROOT / "packaging/appimage/ffmpeg-configure.txt").read_text().split()
    assert {"--enable-ffnvcodec", "--enable-nvdec"} <= set(options)
    assert not {"--enable-nvenc", "--enable-cuvid", "--enable-cuda-nvcc", "--enable-cuda-llvm"} & set(options)
    script = (ROOT / "packaging/appimage/build_ffmpeg.sh").read_text()
    assert (
        "nv-codec-headers|13.1.15.0|https://github.com/FFmpeg/nv-codec-headers/releases/download/n13.1.15.0/"
        "nv-codec-headers-13.1.15.0.tar.gz|52532ceade3d5c1af62624986f13cf01b63c910576b08c0c278756c5e4b41ad0"
    ) in script
    assert 'make -C "${sources[nv-codec-headers]}" PREFIX="$prefix" install' in script
    assert "nv-codec-headers-MIT.txt" in script
    audit = (ROOT / "packaging/appimage/audit_ffmpeg.py").read_text()
    assert '"cuda" not in hardware' in audit


def test_the_private_ffmpeg_is_rebuilt_when_its_script_or_options_change(tmp_path: Path) -> None:
    import runpy

    builder = runpy.run_path(str(ROOT / "packaging/appimage/build_appimage.py"))
    current, record = builder["ffmpeg_current"], builder["record_ffmpeg_inputs"]
    assert not current(tmp_path)  # never built
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/ffmpeg").write_text("")
    assert not current(tmp_path)  # built before its inputs were recorded
    record(tmp_path)
    assert current(tmp_path)
    (tmp_path / "share/mcrawsome/ffmpeg-inputs.sha256").write_text("an older script\n")
    assert not current(tmp_path)
