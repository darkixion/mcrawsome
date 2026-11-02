"""GNOME Files menu provider. Uses the host runtime and passes paths, never recording contents."""

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GObject, Nautilus  # noqa: E402
from mcrawsome_columns import mounted_recordings  # noqa: E402


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


def notify(identity, title, message):
    application = Gio.Application.get_default()
    if application is not None:
        notification = Gio.Notification.new(title)
        notification.set_body(message[:2000])
        application.send_notification(identity, notification)


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
        # As in the app's menu: Unmount while any selected media is mounted, disabled while any of it is open.
        states = mounted_recordings()
        selected = [states.get(os.path.realpath(path)) for path in paths]
        verb = "unmount" if any(selected) else "mount"
        toggle = Nautilus.MenuItem(
            name=f"MCRAWsome::Resolve::{verb}", label=verb.capitalize(), sensitive="in-use" not in selected
        )
        toggle.connect("activate", self.change_mounts, verb, paths)
        menu.append_item(toggle)
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

    def change_mounts(self, item, verb, paths):
        starting = (
            "Mounting recordings. The MCRAWsome window does not need to open." if verb == "mount" else None
        )
        self.run([verb, "--", *paths], "mcrawsome-mounts", f"{verb}ed", verb, starting)

    def send(self, item, mode, paths):
        self.run(
            ["send-to-resolve", "--mode", mode, "--", *paths],
            "mcrawsome-resolve-send",
            "sent to Resolve",
            "send to Resolve",
            "Preparing recordings for Resolve. The MCRAWsome window does not need to open.",
        )

    def run(self, arguments, identity, done, action, starting):
        failed = f"MCRAWsome: could not {action}"
        try:
            if not menu_enabled():
                return
            executable = "/usr/bin/mcrawsome"
            if not os.access(executable, os.X_OK):
                raise ValueError("Install the MCRAWsome Debian package first.")
            process = Gio.Subprocess.new(
                [executable, *arguments],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            if starting is not None:
                notify(identity, "MCRAWsome", starting)
            process.communicate_utf8_async(
                None, None, self.finished, (identity, f"MCRAWsome: {done}", failed)
            )
        except Exception as exc:
            notify(identity, failed, str(exc))

    def finished(self, process, result, data):
        identity, done, failed = data
        try:
            _, output, error = process.communicate_utf8_finish(result)
            if process.get_successful():
                notify(identity, done, output.strip().splitlines()[-1] if output.strip() else "")
            else:
                notify(identity, failed, (error.strip() or output.strip()).removeprefix("Error: "))
        except Exception as exc:
            notify(identity, failed, str(exc))
        self.emit_items_updated_signal()  # the selection's Mount or Unmount may have changed
