"""GNOME Files menu provider. Uses the host runtime and passes paths, never recording contents."""

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GObject, Nautilus  # noqa: E402


def configuration():
    value = os.environ.get("XDG_DATA_HOME", "")
    data = Path(value) if os.path.isabs(value) else Path.home() / ".local/share"
    return data / "mcrawsome/file-manager-actions.json"


def menu_enabled():
    try:
        saved = json.loads(configuration().read_text())
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    if not isinstance(saved, dict):
        return False
    return saved.get("version") == 3 and saved.get("enabled") is True


def notify(title, message):
    application = Gio.Application.get_default()
    if application is not None:
        notification = Gio.Notification.new(title)
        notification.set_body(message[:2000])
        application.send_notification("mcrawsome-resolve-send", notification)


class MCRAWsomeMenu(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, *args):
        files = args[-1]
        if not files or len(files) > 64:
            return []
        paths = []
        for item in files:
            uri = urlsplit(item.get_uri())
            path = unquote(uri.path)
            if (
                item.is_directory()
                or uri.scheme != "file"
                or uri.netloc not in ("", "localhost")
                or not path.startswith("/")
                or "\0" in path
                or Path(path).suffix.lower() != ".mcraw"
            ):
                return []
            paths.append(path)
        if not menu_enabled():
            return []
        root = Nautilus.MenuItem(name="MCRAWsome::Resolve", label="MCRAWsome")
        menu = Nautilus.Menu()
        for mode, label in (
            ("bin", "Send to Resolve (Media Bin)"),
            ("timeline", "Send to Resolve (Timeline)"),
        ):
            action = Nautilus.MenuItem(name=f"MCRAWsome::Resolve::{mode}", label=label)
            action.connect("activate", self.send, mode, paths)
            menu.append_item(action)
        root.set_submenu(menu)
        return [root]

    def get_background_items(self, *args):
        return []

    def send(self, item, mode, paths):
        try:
            if not menu_enabled():
                return
            executable = "/usr/bin/mcrawsome"
            if not os.access(executable, os.X_OK):
                raise ValueError("Install the MCRAWsome Debian package before sending to Resolve.")
            process = Gio.Subprocess.new(
                [executable, "send-to-resolve", "--mode", mode, "--", *paths],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            notify(
                "MCRAWsome", "Preparing recordings for Resolve. The MCRAWsome window does not need to open."
            )
            process.communicate_utf8_async(None, None, self.finished, None)
        except Exception as exc:
            notify("MCRAWsome: could not send to Resolve", str(exc))

    def finished(self, process, result, data):
        try:
            _, output, error = process.communicate_utf8_finish(result)
            successful = process.get_successful()
            message = (
                (output.strip().splitlines()[-1] if output.strip() else "Complete the import in Resolve.")
                if successful
                else (error.strip() or output.strip() or "The send failed.")
            )
            notify(
                "MCRAWsome: sent to Resolve" if successful else "MCRAWsome: could not send to Resolve",
                message,
            )
        except Exception as exc:
            notify("MCRAWsome: could not send to Resolve", str(exc))
