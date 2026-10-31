"""Choosing the GTK 4 window: never requiring GTK 4 in a process that then needs GTK 3."""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def test_webkit_is_checked_before_gtk_4_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.gtk4 import window

    required: list[tuple[str, str]] = []

    def require_version(name: str, version: str) -> None:
        if name == "WebKit":
            raise ValueError("Namespace WebKit not available")
        required.append((name, version))

    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(require_version=require_version))
    assert not window.available()
    assert required == []  # GTK 3 can still be loaded: nothing asked for GTK 4

    monkeypatch.setitem(
        sys.modules, "gi", SimpleNamespace(require_version=lambda *args: required.append(args))
    )
    assert window.available()
    assert required == list(window.VERSIONS)


def test_a_drop_forwards_local_paths_only() -> None:
    from mcrawsome.gtk4.window import local_paths

    files = SimpleNamespace(
        get_files=lambda: [
            SimpleNamespace(get_path=lambda: "/clips/a.mcraw"),
            SimpleNamespace(get_path=lambda: None),
        ]
    )
    assert local_paths(files) == ["/clips/a.mcraw"]


def test_the_gtk_4_window_is_chosen_only_when_asked_and_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome import gui
    from mcrawsome.gtk4 import window

    opened: list[str] = []
    monkeypatch.setattr(window, "open_window", lambda url, bridge, **kwargs: opened.append(url))
    monkeypatch.setattr(window, "available", lambda: True)
    monkeypatch.setitem(sys.modules, "webview", None)  # the GTK 3 window would raise ImportError
    bridge = gui.Bridge(8642, [Path("/library")])
    monkeypatch.setenv("MCRAWSOME_GTK", "4")
    gui._open_window("http://127.0.0.1:8642/", bridge)
    assert opened == ["http://127.0.0.1:8642/"]

    monkeypatch.setattr(window, "available", lambda: False)  # falls back to the GTK 3 window
    with pytest.raises(ImportError):
        gui._open_window("http://127.0.0.1:8642/", bridge)
    monkeypatch.delenv("MCRAWSOME_GTK")
    monkeypatch.setattr(window, "available", lambda: pytest.fail("GTK 4 was not asked for"))
    with pytest.raises(ImportError):
        gui._open_window("http://127.0.0.1:8642/", bridge)
    assert opened == ["http://127.0.0.1:8642/"]


def test_the_bridge_uses_the_gtk_4_hosts_fullscreen_and_viewport() -> None:
    from mcrawsome import gui

    bridge = gui.Bridge(8642, [])
    asked: list[str] = []

    def fullscreen(element: str) -> bool:
        asked.append(element)
        return True

    bridge._window = SimpleNamespace(request_fullscreen=fullscreen)
    assert bridge.request_fullscreen("preview-surface") is True
    assert asked == ["preview-surface"]
    made: list[Any] = []

    def viewport(window: Any) -> Any:
        made.append(window)
        return SimpleNamespace(install=lambda: None)

    bridge._viewport_factory = viewport
    bridge.prepare_preview()
    assert made == [bridge._window]
    assert "_viewport_factory" not in [name for name in dir(bridge) if not name.startswith("_")]
