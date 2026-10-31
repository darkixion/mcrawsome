"""The window as the desktop's media player (MPRIS), so media keys and GNOME's media controls play and
pause the preview. The page says what the preview shows; the desktop's commands go back to it."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

BUS_NAME = "org.mpris.MediaPlayer2.mcrawsome"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
INTERFACES = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""
COMMANDS = {"Play": "play", "Pause": "pause", "PlayPause": "play-pause", "Stop": "stop"}  # to the page
ROOT: dict[str, tuple[str, Any]] = {
    "CanQuit": ("b", False),
    "CanRaise": ("b", True),
    "HasTrackList": ("b", False),
    "Identity": ("s", "MCRAWsome"),
    "DesktopEntry": ("s", "mcrawsome"),
    "SupportedUriSchemes": ("as", []),
    "SupportedMimeTypes": ("as", []),
}


def track(clip: str | None) -> str:
    """The clip's MPRIS track: an object path, so only letters, digits and underscores."""
    return (
        NO_TRACK
        if clip is None
        else "/io/github/darkixion/mcrawsome/clip/" + re.sub(r"\W", "_", clip, flags=re.ASCII)
    )


@dataclass(frozen=True)
class Playback:
    """What the preview shows: playing or not, and which clip, when one is open."""

    playing: bool = False
    title: str | None = None
    clip: str | None = None

    def properties(self) -> dict[str, tuple[str, Any]]:
        """The Player interface's properties, as (type, value). The preview has no seeking, rate or volume
        here: the window keeps those."""
        loaded = self.clip is not None
        metadata: dict[str, tuple[str, Any]] = {"mpris:trackid": ("o", track(self.clip))}
        if self.title:
            metadata["xesam:title"] = ("s", self.title)
        return {
            "PlaybackStatus": ("s", ("Playing" if self.playing else "Paused") if loaded else "Stopped"),
            "Rate": ("d", 1.0),
            "Metadata": ("a{sv}", metadata),
            "Volume": ("d", 1.0),
            "Position": ("x", 0),
            "MinimumRate": ("d", 1.0),
            "MaximumRate": ("d", 1.0),
            "CanGoNext": ("b", False),
            "CanGoPrevious": ("b", False),
            "CanPlay": ("b", loaded),
            "CanPause": ("b", loaded),
            "CanSeek": ("b", False),
            "CanControl": ("b", True),
        }


class MediaPlayer:
    """Publishes the preview's playback on the session bus and passes the desktop's commands on."""

    def __init__(self, command: Callable[[str], None], show: Callable[[], None]) -> None:
        self.command, self.show = command, show
        self.gio = importlib.import_module("gi.repository.Gio")
        self.glib = importlib.import_module("gi.repository.GLib")
        self.state = Playback()
        self.connection: Any = None

    def start(self) -> None:
        """Own the bus name on GLib's main loop, the window's."""
        node = self.gio.DBusNodeInfo.new_for_xml(INTERFACES)

        def acquired(connection: Any, name: str) -> None:
            self.connection = connection
            for interface in node.interfaces:
                connection.register_object(OBJECT_PATH, interface, self.called, self.read, None)

        self.gio.bus_own_name(
            self.gio.BusType.SESSION, BUS_NAME, self.gio.BusNameOwnerFlags.NONE, acquired, None, None
        )

    def called(
        self,
        connection: Any,
        sender: str,
        path: str,
        interface: str,
        method: str,
        parameters: Any,
        invocation: Any,
    ) -> None:
        if method in COMMANDS:
            self.command(COMMANDS[method])
        elif method == "Raise":
            self.show()
        invocation.return_value(None)  # Next, Previous, Seek and the rest do nothing here

    def read(self, connection: Any, sender: str, path: str, interface: str, name: str) -> Any:
        signature, value = (ROOT if interface == "org.mpris.MediaPlayer2" else self.state.properties())[name]
        return self.variant(signature, value)

    def variant(self, signature: str, value: Any) -> Any:
        if signature == "a{sv}":
            return self.glib.Variant("a{sv}", {key: self.variant(*item) for key, item in value.items()})
        return self.glib.Variant(signature, value)

    def update(self, playing: bool, title: str | None, clip: str | None) -> None:
        """The page's playback changed; from any thread: the bus is told on the main loop."""
        self.glib.idle_add(self.changed, Playback(bool(playing), title or None, clip or None))

    def changed(self, state: Playback) -> bool:
        before, after = self.state.properties(), state.properties()
        self.state = state
        changed = {name: self.variant(*value) for name, value in after.items() if value != before[name]}
        if changed and self.connection is not None:
            self.connection.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                self.glib.Variant("(sa{sv}as)", (PLAYER, changed, [])),
            )
        return False  # once
