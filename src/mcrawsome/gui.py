"""One desktop window on the local engine, with a browser fallback."""

from __future__ import annotations

import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from http.client import HTTPException
from pathlib import Path
from typing import Any

from mcrawsome import __version__
from mcrawsome.appimage import host_environment, launcher, running_image
from mcrawsome.brand import DESKTOP_AGENT, PRODUCT
from mcrawsome.build import build_id
from mcrawsome.camera.vignette import VignetteProfile
from mcrawsome.gui_drop import attach_file_drop
from mcrawsome.gui_instance import GuiInstance
from mcrawsome.library import SETTLE_S
from mcrawsome.notifications import notify
from mcrawsome.paths import SESSION_SUFFIX, state_dir
from mcrawsome.preview.display import DisplayLease
from mcrawsome.processes import process_id, spawn, stop_unit
from mcrawsome.server.config import load_config
from mcrawsome.viewport import NativeViewport, Rectangle
from mcrawsome.webkit import attach_shutdown, request_fullscreen

HEALTH = "/api/v1/health"
START_S = 15.0
OFFER_POLL_S = (
    5.0  # how often a window on an engine from another build checks whether its mounts are released
)
TITLE = PRODUCT

log = logging.getLogger(__name__)
OpenWindow = Callable[[str, "Bridge | None"], None]


def _headers() -> dict[str, str]:
    token = load_config({"token": os.environ.get("MCRAWSOME_SERVER_TOKEN")}).token
    return {"Authorization": f"Bearer {token}"} if token else {}


def owns_engine(port: int, process: subprocess.Popen[bytes]) -> bool:
    """Only the process named by health belongs to this window."""
    try:
        return process.poll() is None and request(port, "/health").get("pid") == process_id(process)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def rotate_log(path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    """Keep one previous engine log when the current file reaches the limit."""
    try:
        if path.stat().st_size >= max_bytes:
            path.replace(path.with_suffix(".log.1"))
    except FileNotFoundError:
        pass


def answers(port: int, timeout: float = 1.0) -> bool:
    """A mcrawsome server answers GET /api/v1/health on 127.0.0.1:`port`."""
    try:
        query = urllib.request.Request(f"http://127.0.0.1:{port}{HEALTH}", headers=_headers())
        with urllib.request.urlopen(query, timeout=timeout) as response:
            return response.status == 200 and "version" in json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False


def request(
    port: int,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """A local engine request; failures leave the engine running."""
    data = json.dumps(body).encode() if body is not None else None
    query = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1{path}",
        method=method,
        data=data,
        headers={"Content-Type": "application/json"} | _headers(),
    )
    with urllib.request.urlopen(
        query,
        timeout=timeout
        if timeout is not None
        else (120 if path == "/opened-clips" and method == "POST" else 2),
    ) as response:
        loaded: dict[str, Any] = json.loads(response.read())
        return loaded


def start_server(port: int, roots: list[Path]) -> subprocess.Popen[bytes]:
    """Start a detached engine and wait up to fifteen seconds for health. From the AppImage it is started
    through the image, so it keeps its own copy of the program when this window's copy goes."""
    image = launcher()
    program = [image] if image else [sys.executable, "-m", "mcrawsome.cli.main"]
    command = [*program, "server", "--port", str(port)]
    for root in roots:
        command += ["--library", str(root)]
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    rotate_log(folder / "server.log")
    with (folder / "server.log").open("ab") as output:
        server = spawn(
            command,
            role="engine",
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + START_S
    while time.monotonic() < deadline:
        if answers(port):
            threading.Thread(target=server.wait, name="engine-wait", daemon=True).start()
            return server
        if server.poll() is not None:
            # Another gui may have won the race to bind the port.
            if answers(port):
                return server
            raise RuntimeError(
                f"the server stopped (exit status {server.returncode}); see {folder / 'server.log'}"
            )
        time.sleep(0.2)
    if not stop_unit(server):
        server.terminate()
    server.wait(10)
    raise RuntimeError(f"the server did not answer on port {port} within {START_S:.0f} s")


def open_page(url: str) -> None:
    """Open `url` in the user's browser. Inside the AppImage the browser is started through xdg-open
    with the host's environment, never with the image's libraries."""
    if launcher() is None:
        webbrowser.open(url)
        return
    browser = subprocess.Popen(
        ["xdg-open", url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=host_environment(),
    )
    threading.Thread(target=browser.wait, name="browser-wait", daemon=True).start()


def offer_desktop_entry(window: Any) -> None:
    """From the AppImage, offer once for each image and registration to add it to the application menu
    and register it for .mcraw recordings."""
    if running_image() is None:
        return
    from mcrawsome.desktop import desktop_entry, desktop_entry_status
    from mcrawsome.server.settings import load_settings, save_settings

    settings, problem = load_settings()
    status = desktop_entry_status()
    if problem is not None or status is None:
        return
    identity, current = status
    if current or settings.desktop_entry_offer == f"{identity}:declined":
        return
    accepted = window.create_confirmation_dialog(
        TITLE,
        f"Add or update {PRODUCT} in your application menu and register this AppImage for .mcraw recordings?",
    )
    if accepted:
        desktop_entry()
    decision = "installed" if accepted else "declined"
    save_settings(settings.model_copy(update={"desktop_entry_offer": f"{identity}:{decision}"}))


def this_build() -> str:
    return f"MCRAWsome {__version__} (build {build_id()})"


def other_build(port: int) -> str | None:
    """The engine's version and build when they differ from this program's, or None when they match or
    cannot be read (an engine that does not report a build is an earlier one)."""
    try:
        health = request(port, "/health")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    version, build = health.get("version"), health.get("build")
    if not isinstance(version, str):
        return None
    if version == __version__ and build == build_id():
        return None
    return f"MCRAWsome {version} ({f'build {build}' if isinstance(build, str) else 'an earlier build'})"


def active_media(port: int) -> int:
    for attempt in range(2):
        try:
            return int(request(port, "/health").get("active_mounts", 0))
        except (OSError, HTTPException):
            if attempt:
                raise
            time.sleep(0.1)
    raise AssertionError("unreachable")


def replace_engine(port: int, roots: list[Path]) -> subprocess.Popen[bytes]:
    """Stop the idle engine answering on `port` and start this program's."""
    request(port, "/engine/stop", "POST")
    deadline = time.monotonic() + START_S
    while answers(port):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"the engine on port {port} did not stop within {START_S:.0f} s")
        time.sleep(0.2)
    return start_server(port, roots)


class Bridge:
    """The window's native folder actions and engine restart."""

    def __init__(self, port: int, roots: list[Path]) -> None:
        self.port = port
        self.roots = roots
        self._window: Any = None
        self._instance: GuiInstance | None = None
        self.owned = False
        self.stale: str | None = None  # the engine's version and build when they are not this program's
        self._restart = threading.Lock()
        self._preview: NativeViewport | None = None
        self._preview_lock = threading.Lock()
        # Private: the page may call every public method. The GTK 4 window replaces it with its own overlay's.
        self._viewport_factory: Callable[[Any], NativeViewport] = NativeViewport

    def _native_preview(self) -> NativeViewport:
        with self._preview_lock:
            if self._preview is None:
                self._preview = self._viewport_factory(self._window)
            return self._preview

    def prepare_preview(self) -> None:
        """On GTK's main loop before the window is shown, so the first clip opened does not flicker."""
        # Without the surface the page shows its own picture, as when it is unavailable later.
        with suppress(ImportError, ValueError, RuntimeError, AttributeError):
            self._native_preview().install()

    def preview_available(self, owner: int) -> bool:
        if type(owner) is not int or not 0 <= owner <= 2**53 - 1:
            return False
        try:
            return self._native_preview().available(owner)
        except (ImportError, ValueError, RuntimeError):
            return False

    def preview_present(
        self,
        owner: int,
        epoch: int,
        display: dict[str, Any],
        rect: dict[str, Any],
        index: int = 0,
    ) -> bool | None:
        if not self._preview_request(owner, epoch) or type(index) is not int or not 0 <= index < 2**31:
            return False
        try:
            lease, bounds = DisplayLease.model_validate(display), Rectangle.model_validate(rect)
            assert self._preview is not None
            return self._preview.present(owner, epoch, lease, bounds, index)
        except (OSError, ValueError, RuntimeError):
            return False

    def preview_bounds(self, owner: int, epoch: int, rect: dict[str, Any]) -> bool:
        if not self._preview_request(owner, epoch):
            return False
        try:
            bounds = Rectangle.model_validate(rect)
            assert self._preview is not None
            return self._preview.bounds(owner, epoch, bounds)
        except (ValueError, RuntimeError):
            return False

    def preview_hide(self, owner: int, epoch: int) -> bool:
        if not self._preview_request(owner, epoch):
            return False
        assert self._preview is not None
        return self._preview.hide(owner, epoch)

    def preview_overlay(self, owner: int, revision: int, model: dict[str, Any] | None) -> bool:
        if not self._preview_request(owner, revision):
            return False
        try:
            from mcrawsome.preview.chart import ChartModel

            assert self._preview is not None
            return self._preview.chart(
                owner, revision, ChartModel.model_validate(model) if model is not None else None
            )
        except (ValueError, RuntimeError):
            return False

    def preview_crop(self, owner: int, revision: int, outline: dict[str, Any] | None) -> bool:
        if not self._preview_request(owner, revision):
            return False
        try:
            from mcrawsome.viewport import CropOutline

            assert self._preview is not None
            return self._preview.crop(
                owner, revision, CropOutline.model_validate(outline) if outline is not None else None
            )
        except (ValueError, RuntimeError):
            return False

    def _preview_request(self, owner: int, epoch: int) -> bool:
        return (
            self._preview is not None
            and type(owner) is int
            and 0 <= owner <= 2**53 - 1
            and type(epoch) is int
            and 0 <= epoch <= 2**53 - 1
        )

    def request_fullscreen(self, element_id: str) -> bool:
        own = getattr(self._window, "request_fullscreen", None)  # the GTK 4 window's host
        return bool(own(element_id)) if own is not None else request_fullscreen(self._window, element_id)

    def choose_folder(self) -> str | None:
        import webview  # type: ignore[import-not-found, unused-ignore]

        selected = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return str(selected[0]) if selected else None

    def choose_session(self, save: bool) -> str | None:
        """Choose a named Library workspace file; the engine reads and writes its contents.

        A name typed in the save dialog gets the session suffix the engine requires: the dialog offers
        Untitled.mcrawsome-session.json with only the part before its last dot selected to type over."""
        import webview  # type: ignore[import-not-found, unused-ignore]

        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE if save else webview.FileDialog.OPEN,
            save_filename=f"Untitled{SESSION_SUFFIX}" if save else "",
            file_types=("MCRAWsome sessions (*.json)",),
        )
        if not chosen:
            return None
        path = Path(chosen[0])
        if save and not path.name.endswith(SESSION_SUFFIX):
            name = path.name.removesuffix(".json").removesuffix(SESSION_SUFFIX.removesuffix(".json"))
            path = path.with_name(name + SESSION_SUFFIX)
            # The dialog asked about replacing the name chosen, not this one.
            if path.exists() and not self._window.create_confirmation_dialog(TITLE, f"Replace {path.name}?"):
                return None
        return str(path)

    def choose_files(self) -> list[str]:
        import webview  # type: ignore[import-not-found, unused-ignore]

        selected = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=("MotionCam clips (*.mcraw;*.mp4;*.mov;*.mkv)",),
        )
        return [str(path) for path in selected] if selected else []

    def load_vignette_profile(self) -> dict[str, Any] | None:
        import webview  # type: ignore[import-not-found, unused-ignore]

        selected = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False, file_types=("Vignette profile (*.json)",)
        )
        if not selected:
            return None
        with Path(selected[0]).open("rb") as file:
            content = file.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("Vignette profile exceeds 2 MiB")
        return VignetteProfile.model_validate_json(content).model_dump(mode="json")

    def save_vignette_profile(self, profile: dict[str, Any], name: str) -> bool:
        checked = VignetteProfile.model_validate(profile)
        return self._save_json(checked.model_dump_json(indent=2) + "\n", f"{Path(name).name}.vignette.json")

    def save_calibration_target(self) -> bool:
        from mcrawsome.camera.calibrate_lens import target_svg

        return self._save_document(target_svg(), "mcrawsome-lens-target.svg", "SVG target (*.svg)")

    def save_camera_profile(self, name: str) -> bool:
        import tomli_w

        from mcrawsome.camera.profile import all_profiles

        all_profiles.cache_clear()
        for profile, _mine in all_profiles():
            if profile.name == name:
                content = tomli_w.dumps(profile.model_dump(mode="json", exclude_none=True))
                return self._save_document(content, f"{name}.toml", "Camera profile (*.toml)")
        raise ValueError("Camera profile does not exist")

    def save_report(self, content: str, name: str) -> bool:
        if len(content.encode("utf-8")) > 8 * 1024 * 1024:
            raise ValueError("JSON report exceeds 8 MiB")
        try:
            json.loads(content)
        except ValueError:
            raise ValueError("Report must be valid JSON") from None
        return self._save_json(content, Path(name).name)

    def save_capture_csv(self, identity: str, name: str) -> bool:
        """Stream a metadata report from this engine to the user's chosen file."""
        import webview  # type: ignore[import-not-found, unused-ignore]

        if not identity or len(identity) > 128 or not all(c.isalnum() or c in "-_" for c in identity):
            raise ValueError("Invalid clip identity")
        selected = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=f"{Path(name).name}.capture.csv",
            file_types=("Frame metadata (*.csv)",),
        )
        if not selected:
            return False
        target = Path(selected[0])
        temporary: Path | None = None
        try:
            clip_key = urllib.parse.quote(identity, safe="")
            url = f"http://127.0.0.1:{self.port}/api/v1/clips/{clip_key}/capture.csv"
            with urllib.request.urlopen(url, timeout=30) as response:
                if not response.headers.get("Content-Type", "").startswith("text/csv"):
                    raise ValueError("The engine did not return a metadata CSV")
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix=".mcrawsome-capture-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    shutil.copyfileobj(response, stream, length=64 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            return True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save_text(self, content: str, name: str) -> bool:
        """Save a camera report (CSV or Avid ALE) or clip positions (GPX) the engine made, where the user
        chooses."""
        kinds = {
            ".csv": "Camera report (*.csv)",
            ".ale": "Avid Log Exchange (*.ale)",
            ".gpx": "GPS exchange (*.gpx)",
        }
        name = Path(name).name
        kind = kinds.get(Path(name).suffix.lower())
        if kind is None:
            raise ValueError("Only CSV, ALE and GPX lists can be saved")
        if len(content.encode("utf-8")) > 8 * 1024 * 1024:
            raise ValueError("The list exceeds 8 MiB")
        return self._save_document(content, name, kind)

    def _save_json(self, content: str, name: str) -> bool:
        kind = "Clip metadata (*.meta)" if name.endswith(".meta") else "JSON report (*.json)"
        return self._save_document(content, name, kind)

    def _save_document(self, content: str, name: str, kind: str) -> bool:
        import webview  # type: ignore[import-not-found, unused-ignore]

        selected = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=name,
            file_types=(kind,),
        )
        if not selected:
            return False
        Path(selected[0]).write_text(content, encoding="utf-8")
        return True

    def file_menu_status(self) -> dict[str, Any]:
        from mcrawsome.desktop_actions import menu_status

        return dict(menu_status())

    def set_file_menu(self, enabled: bool) -> dict[str, Any]:
        from mcrawsome.desktop_actions import menu_status, set_menu

        if type(enabled) is not bool:
            raise ValueError("Choose whether the file-manager menu is enabled")
        status = menu_status()
        install_support = enabled and status["requires_admin"]
        if install_support and (
            self._window is None
            or not self._window.create_confirmation_dialog(
                TITLE,
                "Administrator access is required to install Nautilus support (python3-nautilus). "
                "The system will ask for your password. Install it and enable MCRAWsome's menu?",
            )
        ):
            return {"ok": False, "status": status, "cancelled": True}
        try:
            return {"ok": True, "status": dict(set_menu(enabled, install_support=install_support))}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "status": dict(menu_status()), "message": str(exc)}

    def file_association(self) -> bool:
        from mcrawsome.desktop_actions import file_association

        return file_association()

    def associate_files(self) -> bool:
        from mcrawsome.desktop_actions import associate_files

        return associate_files()

    def media_available(self) -> bool:
        """Check WebKit's required media elements before the page creates an audio element.

        pywebview runs bridge calls on worker threads, outside GTK's main loop.
        """
        try:
            gi = importlib.import_module("gi")
            gi.require_version("Gst", "1.0")
            gst = importlib.import_module("gi.repository.Gst")
            gst.init(None)
            return all(
                gst.ElementFactory.find(name) is not None
                for name in (
                    "appsink",
                    "autoaudiosink",
                    "playbin",
                    "wavparse",
                    "audioconvert",
                    "audioresample",
                    "fakevideosink",
                    "scaletempo",
                    "webvttenc",
                )
            )
        except (ImportError, ValueError, RuntimeError):
            return False

    def open_folder(self, path: str) -> None:
        folder = Path(path).resolve(strict=True)
        if not folder.is_dir():
            raise ValueError(f"{folder} is not a folder")
        subprocess.run(["xdg-open", str(folder)], check=True, env=host_environment())

    def reveal_file(self, path: str) -> None:
        """Ask the session's file manager to select this file, keeping its displayed symlink path."""
        source = Path(path).absolute()
        if not source.is_file():
            raise ValueError(f"{source} is not a file")
        subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.FileManager1",
                "--object-path",
                "/org/freedesktop/FileManager1",
                "--method",
                "org.freedesktop.FileManager1.ShowItems",
                json.dumps([source.as_uri()]),
                "",
            ],
            check=True,
            capture_output=True,
            timeout=5,
            env=host_environment(),
        )

    def notify(self, summary: str, body: str) -> bool:
        """Show a desktop notification, as MCRAWsome's; False when the desktop has no notification
        service."""
        return notify(summary, body)

    def restart_engine(self) -> None:
        with self._restart:
            if not answers(self.port):
                process = start_server(self.port, self.roots)
                self.owned = owns_engine(self.port, process)


def offer_restart(
    bridge: Bridge, window: Any, url: str, *, wait: Callable[[float], bool] | None = None
) -> None:
    """Wait until the stale engine's mounts are released, then offer to restart it with this build and reload
    the page. `wait(seconds)` returns True to give up."""
    wait = wait or threading.Event().wait
    while bridge.stale is not None:
        try:
            if active_media(bridge.port) == 0:
                break
        except (OSError, ValueError, KeyError, urllib.error.URLError, HTTPException):
            return
        if wait(OFFER_POLL_S):
            return
    running = bridge.stale
    if running is None:
        return
    question = (
        f"The engine running is {running}, not this {this_build()}. It has no active mounts: restart it now?"
    )
    if not window.create_confirmation_dialog(TITLE, question):
        return
    with bridge._restart:
        process = replace_engine(bridge.port, bridge.roots)
        bridge.owned = owns_engine(bridge.port, process)
        bridge.stale = None
    window.load_url(url)


class WindowUnavailableError(RuntimeError):
    """GTK or WebKitGTK could not create a window."""


def _open_window(url: str, bridge: Bridge | None) -> None:
    if os.environ.get("MCRAWSOME_GTK") == "4" and bridge is not None:
        from mcrawsome.gtk4.window import available, open_window

        if available():
            open_window(url, bridge, title=TITLE, user_agent=DESKTOP_AGENT)
            return
        log.warning("The GTK 4 window needs WebKitGTK 6.0 (gir1.2-webkit-6.0): using the GTK 3 window")
    try:
        webview = importlib.import_module("webview")
        gi = importlib.import_module("gi")
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        window = webview.create_window(
            TITLE, url, width=1440, height=900, min_size=(1024, 700), js_api=bridge
        )
    except ImportError:
        raise
    except (ValueError, RuntimeError) as exc:
        raise WindowUnavailableError(str(exc)) from exc
    if bridge is not None:
        bridge._window = window
        if bridge._instance is not None:
            window.events.loaded += lambda: bridge._instance.attach(window) if bridge._instance else None
        window.events.before_show += lambda: attach_file_drop(window)
        window.events.before_show += bridge.prepare_preview
        if bridge.stale is not None:  # once, not on every page load
            offer = threading.Thread(target=offer_restart, args=(bridge, window, url), daemon=True)
            window.events.loaded += lambda: None if offer.ident is not None else offer.start()
    window.events.before_show += attach_shutdown
    if running_image() is not None:
        window.events.loaded += lambda: offer_desktop_entry(window)
    webview.start(gui="gtk", user_agent=DESKTOP_AGENT)


def show(
    url: str,
    say: Callable[[str], None],
    bridge: Bridge | None = None,
    *,
    open_window: OpenWindow | None = None,
) -> bool:
    """Open the window until closed, or open the default browser if GTK cannot start."""
    try:
        (open_window or _open_window)(url, bridge)
    except (ImportError, WindowUnavailableError) as exc:
        missing = (
            "pywebview" if isinstance(exc, ImportError) and exc.name == "webview" else "GTK or WebKitGTK"
        )
        if isinstance(exc, ImportError) and exc.name == "gi":
            missing = "gi"
        say(f"cannot open the {missing} window ({exc}): opening the page in the default browser")
        open_page(url)
        return False
    return True


def launch(
    port: int,
    roots: list[Path],
    say: Callable[[str], None],
    *,
    browser: bool = False,
    clip: Path | None = None,
    clips: tuple[Path, ...] = (),
    view: str | None = None,
    open_window: OpenWindow | None = None,
) -> None:
    files = list(dict.fromkeys([*([clip] if clip is not None else []), *clips]))
    instance = GuiInstance.acquire(port, files, view) if sys.platform == "linux" and not browser else None
    if sys.platform == "linux" and not browser and instance is None:
        return
    try:
        _launch(
            port,
            roots,
            say,
            browser=browser,
            files=files,
            view=view,
            open_window=open_window,
            instance=instance,
        )
    finally:
        if instance is not None:
            instance.close()


def _launch(
    port: int,
    roots: list[Path],
    say: Callable[[str], None],
    *,
    browser: bool,
    files: list[Path],
    view: str | None = None,
    open_window: OpenWindow | None,
    instance: GuiInstance | None,
) -> None:

    bridge = Bridge(port, roots)
    bridge._instance = instance
    if not answers(port):
        say(f"Starting the server on port {port}")
        process = start_server(port, roots)
        bridge.owned = owns_engine(port, process)
    elif (running := other_build(port)) is not None:
        try:
            busy = active_media(port)
        except (OSError, ValueError, KeyError, HTTPException) as exc:
            if answers(port):
                say(f"Could not check the running engine's work ({exc}); keeping it running")
                bridge.stale = running
            else:
                say(f"The previous engine stopped while checking its work; starting this {this_build()}")
                process = start_server(port, roots)
                bridge.owned = owns_engine(port, process)
        else:
            if busy == 0:
                say(f"The engine on port {port} is {running}, not this {this_build()}: restarting it")
                process = replace_engine(port, roots)
                bridge.owned = owns_engine(port, process)
            else:
                say(
                    f"The engine on port {port} is {running}, not this {this_build()}, and has {busy} mount"
                    f"{'s' if busy != 1 else ''} active: using it; "
                    "the window offers to restart it "
                    "when its mounts are released"
                )
                bridge.stale = running
    url = page_url(port, None)
    sessions = [path for path in files if path.name.lower().endswith(SESSION_SUFFIX)]
    recordings = [path for path in files if path not in sessions]
    asked = ["open=1"] if files else []
    if sessions:  # the last one given; recordings opened with it join it
        try:
            request(port, "/session/load", "POST", {"path": str(sessions[-1].absolute())})
        except (OSError, ValueError, urllib.error.URLError):
            # The page's session bar loads it again and says why it cannot.
            asked.append(f"session={urllib.parse.quote(str(sessions[-1].absolute()))}")
    if recordings:
        paths = [str(path.absolute()) for path in recordings]
        try:
            request(port, "/opened-clips", "POST", {"paths": paths})
        except urllib.error.HTTPError as exc:  # refused, with a reason: a video that is not Log, say
            if instance is None:
                raise RuntimeError(f"cannot open recordings: {exc}") from None
            instance.pending.put_nowait((paths, None))  # the page opens them and says why it cannot
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"cannot open recordings: {exc}") from None
    if asked:
        url += f"?{'&'.join(asked)}#/library"
    elif view is not None:
        url += f"#/{view}"
    if browser:
        open_page(url)
        return
    if not show(url, say, bridge, open_window=open_window) or not bridge.owned:
        return
    try:
        if request(port, "/settings")["keep_engine"] or active_media(port):
            return
        request(port, "/engine/stop", "POST")
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        say(f"The engine keeps running: {exc}")


def wait_for_clip(port: int, clip: Path, say: Callable[[str], None]) -> dict[str, Any]:
    target = str(clip.resolve())
    listed = request(port, "/library")["clips"]
    match = next((item for item in listed if item["path"] == target), None)
    if match is None:
        add_folder(port, clip.resolve().parent, say)
    deadline = time.monotonic() + SETTLE_S + 2
    announced = False
    while True:
        if match is not None and match["id"]:
            return dict(match)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{clip.name} is not ready in the library after waiting for it to settle")
        if not announced:
            say(f"Waiting for {clip.name} to finish copying and settle")
            announced = True
        time.sleep(0.2)
        listed = request(port, "/library")["clips"]
        match = next((item for item in listed if item["path"] == target), None)


def add_folder(port: int, folder: Path, say: Callable[[str], None]) -> bool:
    """Add `folder` to the saved library folders, keeping every other setting (a PUT replaces them
    all). False when it is there already."""
    settings = request(port, "/settings")
    problem = settings.pop("problem", None)
    if problem is not None:  # writing the settings back would replace the unreadable file with defaults
        raise RuntimeError(f"cannot add {folder} to the library folders: {problem}")
    if any(folder == Path(root) or folder.parent == Path(root) for root in settings["folders"]):
        return False
    request(port, "/settings", "PUT", settings | {"folders": [*settings["folders"], str(folder)]})
    say(f"Added {folder} to the library folders")
    return True


def page_url(port: int, clip: Path | None) -> str:
    query = f"?path={urllib.parse.quote(str(clip.resolve()))}" if clip is not None else ""
    return f"http://127.0.0.1:{port}/{query}"
