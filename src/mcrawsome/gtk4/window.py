"""The desktop window on GTK 4 and WebKitGTK 6.0: the page in a WebKit view, with the native preview in an
overlay that GTK composites on the GPU (GTK 3 repaints the whole window on the processor for each frame)."""

from __future__ import annotations

import functools
import importlib
import logging
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

from mcrawsome.gui_drop import _forward_paths

if TYPE_CHECKING:
    from mcrawsome.gui import Bridge

log = logging.getLogger(__name__)

SIZE = (1440, 900)
MINIMUM = (1024, 700)
# Checked in this order: WebKit's introspection data is the part a system may lack, and nothing may require
# GTK 4 in a process that then falls back to the GTK 3 window.
VERSIONS = (("WebKit", "6.0"), ("JavaScriptCore", "6.0"), ("Gtk", "4.0"), ("Gdk", "4.0"))


def available() -> bool:
    """GTK 4 and WebKitGTK 6.0 can be loaded (Debian: gir1.2-webkit-6.0 and gir1.2-gtk-4.0)."""
    try:
        gi = importlib.import_module("gi")
        for name, version in VERSIONS:
            gi.require_version(name, version)
    except (ImportError, ValueError) as exc:
        log.info("The GTK 4 window is unavailable: %s", exc)
        return False
    return True


def local_paths(files: Any) -> list[str]:
    """The local paths in a dropped Gdk.FileList; remote locations are left out."""
    return [path for file in files.get_files() if (path := file.get_path())]


def open_window(url: str, bridge: Bridge, *, title: str, user_agent: str) -> None:
    """Show `url` until the window closes, the bridge's methods available to the page as
    `window.pywebview.api`. Call `available()` first."""
    gtk = importlib.import_module("gi.repository.Gtk")
    gdk = importlib.import_module("gi.repository.Gdk")
    gio = importlib.import_module("gi.repository.Gio")
    glib = importlib.import_module("gi.repository.GLib")
    webkit = importlib.import_module("gi.repository.WebKit")
    from mcrawsome.gtk4.bridge import ScriptBridge
    from mcrawsome.gtk4.host import Gtk4Host
    from mcrawsome.gtk4.viewport import NativeViewport4

    # Not unique: GuiInstance already owns the single window per engine port and state folder.
    application = gtk.Application(flags=gio.ApplicationFlags.NON_UNIQUE)
    closing: list[Any] = []

    def activate(app: Any) -> None:
        window = gtk.ApplicationWindow(application=app, title=title)
        window.set_default_size(*SIZE)
        window.set_size_request(*MINIMUM)
        manager = webkit.UserContentManager()
        # Replies ahead of redraws: a playing preview waits for each frame's reply before it frees the slot.
        scripts = ScriptBridge(bridge, functools.partial(glib.idle_add, priority=glib.PRIORITY_DEFAULT))
        scripts.attach(manager)
        view = webkit.WebView(user_content_manager=manager)
        settings = view.get_settings()
        settings.set_user_agent(user_agent)
        settings.set_enable_fullscreen(True)
        settings.set_enable_developer_extras(False)
        settings.set_hardware_acceleration_policy(webkit.HardwareAccelerationPolicy.ALWAYS)
        overlay = gtk.Overlay(child=view)
        window.set_child(overlay)
        host = Gtk4Host(window, view)
        bridge._window = host
        bridge._viewport_factory = lambda _window: NativeViewport4(overlay)
        bridge.prepare_preview()  # before the window shows, so the first clip opened does not flicker
        closing.extend((scripts, host))

        def dropped(_target: Any, files: Any, _x: float, _y: float) -> bool:
            paths = local_paths(files)
            if paths:  # evaluate_js waits for GTK's main loop, so cannot run on it
                threading.Thread(target=_forward_paths, args=(host, paths), daemon=True).start()
            return bool(paths)

        # Ahead of WebKit's own drop handling: the page receives full paths, never file contents.
        drop = gtk.DropTarget.new(gdk.FileList, gdk.DragAction.COPY)
        drop.set_propagation_phase(gtk.PropagationPhase.CAPTURE)
        drop.connect("drop", dropped)
        overlay.add_controller(drop)
        loaded = threading.Event()

        def changed(_view: Any, event: Any) -> None:
            if event != webkit.LoadEvent.FINISHED or loaded.is_set():
                return
            loaded.set()  # once, as pywebview's first `loaded` event
            if bridge._instance is not None:
                bridge._instance.attach(host)
            if bridge.stale is not None:
                from mcrawsome.gui import offer_restart

                threading.Thread(target=offer_restart, args=(bridge, host, url), daemon=True).start()

        view.connect("load-changed", changed)
        engine = urllib.parse.urlsplit(url)[:2]

        def decide(_view: Any, decision: Any, kind: Any) -> bool:
            """Keep the view on the engine's pages, which alone may call the bridge; web links open in the
            default browser."""
            navigation = webkit.PolicyDecisionType.NAVIGATION_ACTION
            if kind not in (navigation, webkit.PolicyDecisionType.NEW_WINDOW_ACTION):
                return False
            target = decision.get_navigation_action().get_request().get_uri()
            parts = urllib.parse.urlsplit(target)
            if kind == navigation and parts[:2] == engine:
                return False
            decision.ignore()
            if parts.scheme in ("http", "https"):
                gtk.UriLauncher.new(target).launch(window, None, None)
            return True

        view.connect("decide-policy", decide)

        def close_request(_window: Any) -> bool:
            for part in closing:
                part.close()
            if bridge._preview is not None:
                bridge._preview.close()
            return False  # the window closes, and the application with it

        window.connect("close-request", close_request)
        view.load_uri(url)
        window.present()

    application.connect("activate", activate)
    application.run(None)
