"""Desktop notifications, shown as MCRAWsome's."""

from __future__ import annotations

import json
import subprocess

from mcrawsome.appimage import host_environment


def notify(summary: str, body: str) -> bool:
    """Show a desktop notification, as MCRAWsome's; False when the desktop has no notification
    service. Values are typed, so a service that cannot describe its method still takes them."""
    try:
        subprocess.run(
            [
                "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                "--object-path", "/org/freedesktop/Notifications",
                "--method", "org.freedesktop.Notifications.Notify",
                '"MCRAWsome"', "uint32 0", '"mcrawsome"',
                json.dumps(summary, ensure_ascii=False), json.dumps(body, ensure_ascii=False),
                "@as []", '@a{sv} {"desktop-entry": <"mcrawsome">}', "int32 -1",
            ],
            check=True,
            capture_output=True,
            timeout=5,
            env=host_environment(),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return True
