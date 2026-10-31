"""Read MCRAW metadata in a child process, keeping Files' main loop responsive."""

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402


def local_recording(files):
    if len(files) != 1 or files[0].is_directory():
        return None
    uri = urlsplit(files[0].get_uri())
    path = unquote(uri.path)
    if (
        uri.scheme != "file"
        or uri.netloc not in ("", "localhost")
        or not path.startswith("/")
        or "\0" in path
        or Path(path).suffix.lower() != ".mcraw"
    ):
        return None
    return path


def property_rows(document):
    if not isinstance(document, dict) or not isinstance(document.get("frames"), int):
        raise ValueError("The recording metadata could not be read.")
    rows = [("Frames", str(document["frames"]))]
    for section, values in document.items():
        if section in {"clip", "frames", "timing"} or not isinstance(values, dict):
            continue
        for label, value in values.items():
            if isinstance(value, str):
                rows.append((f"{section}: {label}", value[:2000]))
    return rows[:100]


class MCRAWsomeProperties(GObject.GObject, Nautilus.PropertiesModelProvider):
    def get_models(self, files):
        path = local_recording(files)
        if path is None:
            return []
        store = Gio.ListStore.new(Nautilus.PropertiesItem)
        store.append(Nautilus.PropertiesItem(name="Recording", value="Reading metadata…"))

        def replace(rows):
            store.remove_all()
            for name, value in rows:
                store.append(Nautilus.PropertiesItem(name=name, value=value))

        try:
            process = Gio.Subprocess.new(
                ["/usr/bin/mcrawsome", "info", "--json", "--", path],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            timed_out = False

            def timeout():
                nonlocal timed_out
                timed_out = True
                process.force_exit()
                replace([("Recording", "Reading metadata timed out. Try mcrawsome info in a terminal.")])
                return False

            timer = GLib.timeout_add_seconds(30, timeout)

            def finished(child, result, data):
                if not timed_out:
                    GLib.source_remove(timer)
                try:
                    _, output, error = child.communicate_utf8_finish(result)
                    if timed_out:
                        return
                    if not child.get_successful():
                        raise ValueError(error.strip() or "The recording metadata could not be read.")
                    replace(property_rows(json.loads(output)))
                except Exception as exc:
                    if not timed_out:
                        replace([("Recording", str(exc)[:2000])])

            process.communicate_utf8_async(None, None, finished, None)
        except Exception as exc:
            replace([("Recording", str(exc)[:2000])])
        return [Nautilus.PropertiesModel(title="MotionCam RAW video", model=store)]
