"""The window is the desktop's media player: media keys and GNOME's media controls play and pause the
preview, and the desktop is told what the preview shows."""

import sys
import threading
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import Any

import pytest

from mcrawsome import gui, mpris
from mcrawsome.mpris import MediaPlayer, Playback, track


class Variant:
    def __init__(self, signature: str, value: Any) -> None:
        self.signature, self.value = signature, value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Variant) and (self.signature, self.value) == (other.signature, other.value)

    def __repr__(self) -> str:
        return f"Variant({self.signature!r}, {self.value!r})"


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> Any:
    registered: list[tuple[str, str]] = []
    signals: list[tuple[Any, ...]] = []
    idle: list[tuple[Any, Any]] = []
    owned: list[str] = []
    connection = SimpleNamespace(
        register_object=lambda path, interface, call, read, write: registered.append((path, interface.name)),
        emit_signal=lambda *signal: signals.append(signal),
    )

    def own(bus_type: Any, name: str, flags: Any, acquired: Any, named: Any, lost: Any) -> int:
        owned.append(name)
        acquired(connection, name)
        return 1

    def parse(xml: str) -> Any:
        names = [node.get("name") for node in ET.fromstring(xml).iter("interface")]
        return SimpleNamespace(interfaces=[SimpleNamespace(name=name) for name in names])

    gio = SimpleNamespace(
        DBusNodeInfo=SimpleNamespace(new_for_xml=parse),
        bus_own_name=own,
        BusType=SimpleNamespace(SESSION="session"),
        BusNameOwnerFlags=SimpleNamespace(NONE=0),
    )
    glib = SimpleNamespace(Variant=Variant, idle_add=lambda callback, value: idle.append((callback, value)))
    monkeypatch.setitem(sys.modules, "gi.repository.Gio", gio)
    monkeypatch.setitem(sys.modules, "gi.repository.GLib", glib)
    return SimpleNamespace(registered=registered, signals=signals, idle=idle, owned=owned)


def test_the_player_says_whether_the_preview_plays_and_what() -> None:
    assert Playback().properties()["PlaybackStatus"] == ("s", "Stopped")
    assert Playback().properties()["CanPlay"] == ("b", False)
    assert Playback().properties()["Metadata"] == ("a{sv}", {"mpris:trackid": ("o", mpris.NO_TRACK)})
    playing = Playback(True, "Harbour.mcraw", "8f9694e4").properties()
    assert playing["PlaybackStatus"] == ("s", "Playing")
    assert playing["CanPlay"] == playing["CanPause"] == ("b", True)
    assert playing["Metadata"] == (
        "a{sv}",
        {
            "mpris:trackid": ("o", "/io/github/darkixion/mcrawsome/clip/8f9694e4"),
            "xesam:title": ("s", "Harbour.mcraw"),
        },
    )
    assert Playback(False, None, "a").properties()["PlaybackStatus"] == ("s", "Paused")


def test_a_clip_becomes_a_valid_object_path() -> None:
    assert track("ab-12.c d/é") == "/io/github/darkixion/mcrawsome/clip/ab_12_c_d__"


def test_commands_reach_the_page_and_raise_shows_the_window(bus: Any) -> None:
    sent: list[str] = []
    shown: list[bool] = []
    player = MediaPlayer(sent.append, lambda: shown.append(True))
    player.start()
    assert bus.owned == [mpris.BUS_NAME]
    assert bus.registered == [
        (mpris.OBJECT_PATH, "org.mpris.MediaPlayer2"),
        (mpris.OBJECT_PATH, "org.mpris.MediaPlayer2.Player"),
    ]
    answered: list[Any] = []
    invocation = SimpleNamespace(return_value=answered.append)
    for method in ("PlayPause", "Play", "Pause", "Stop", "Next", "Raise"):
        player.called(None, "", mpris.OBJECT_PATH, mpris.PLAYER, method, None, invocation)
    assert sent == ["play-pause", "play", "pause", "stop"]
    assert shown == [True]
    assert answered == [None] * 6
    assert player.read(None, "", "", "org.mpris.MediaPlayer2", "Identity") == Variant("s", "MCRAWsome")


def test_only_what_changed_is_announced_on_the_main_loop(bus: Any) -> None:
    player = MediaPlayer(lambda action: None, lambda: None)
    player.start()
    player.update(True, "Harbour.mcraw", "a")
    [(callback, state)] = bus.idle
    assert not bus.signals  # announced from the main loop, not the page's thread
    assert callback(state) is False
    [(_destination, path, interface, name, body)] = bus.signals
    assert (path, interface, name) == (
        mpris.OBJECT_PATH,
        "org.freedesktop.DBus.Properties",
        "PropertiesChanged",
    )
    player_interface, changed, invalidated = body.value
    assert player_interface == mpris.PLAYER
    assert invalidated == []
    assert set(changed) == {"PlaybackStatus", "Metadata", "CanPlay", "CanPause"}
    assert changed["PlaybackStatus"] == Variant("s", "Playing")
    player.changed(Playback(True, "Harbour.mcraw", "a"))
    assert len(bus.signals) == 1  # nothing changed


def test_the_bridge_passes_the_pages_playback_on_and_media_keys_back(bus: Any) -> None:
    bridge = gui.Bridge(9127, [])
    bridge.media_state(True, "Harbour.mcraw", "a")  # before the player: ignored
    scripts: list[str] = []
    delivered = threading.Event()

    def evaluate(script: str) -> None:
        scripts.append(script)
        delivered.set()

    window = SimpleNamespace(evaluate_js=evaluate, restore=lambda: None, show=lambda: None)
    gui.start_media(bridge, window)
    assert bridge._media is not None
    bridge.media_state(True, "Harbour.mcraw", "a")
    assert bus.idle[-1][1] == Playback(True, "Harbour.mcraw", "a")
    bridge._media.called(
        None, "", mpris.OBJECT_PATH, mpris.PLAYER, "PlayPause", None, SimpleNamespace(return_value=print)
    )
    assert delivered.wait(2)
    assert scripts == [
        "window.dispatchEvent(new CustomEvent('mcrawsome-media-key', {detail: \"play-pause\"}));"
    ]
