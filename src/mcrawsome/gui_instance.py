"""One Linux desktop window per engine, with bounded local file-open and view activation."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Self

from mcrawsome.paths import state_dir

log = logging.getLogger(__name__)
_LIMIT = 1024 * 1024
VIEWS = ("library", "settings")  # screens a launcher action can ask the window for


class GuiInstance:
    """An abstract Unix socket is claimed atomically and vanishes even after a crash.

    Its address includes the user, engine port and state directory. Linux peer credentials restrict
    activation to this user; other users cannot ask this window to open paths on their behalf.
    """

    def __init__(self, listener: socket.socket) -> None:
        self.listener = listener
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.pending: queue.Queue[tuple[list[str], str | None]] = queue.Queue(maxsize=32)
        self.window: Any = None
        self.receiver = threading.Thread(target=self._receive, daemon=True, name="desktop-activation")
        self.delivery = threading.Thread(target=self._deliver, daemon=True, name="desktop-open-files")
        self.receiver.start()
        self.delivery.start()

    @classmethod
    def acquire(cls, port: int, paths: list[Path], view: str | None = None) -> Self | None:
        """Own the window slot, or forward the paths and view to its owner and return None before touching
        the engine."""
        scope = f"{state_dir().resolve()}:{port}".encode()
        address = f"\0mcrawsome-ui-{os.getuid()}-{hashlib.sha256(scope).hexdigest()[:32]}"
        asked = {"paths": [str(path.absolute()) for path in paths], "view": view}
        message = json.dumps(asked).encode() + b"\n"
        if len(message) > _LIMIT or len(paths) > 256:
            raise RuntimeError("Open at most 256 recordings at once")
        deadline = time.monotonic() + 5
        while True:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(address)
            except OSError as exc:
                listener.close()
                if exc.errno != errno.EADDRINUSE:
                    raise
            else:
                listener.listen(8)
                listener.settimeout(0.2)
                return cls(listener)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(address)
                    credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    if struct.unpack("3i", credentials)[1] != os.getuid():
                        raise RuntimeError("The desktop activation address belongs to another user")
                    client.sendall(message)
                    if client.recv(32) != b"accepted\n":
                        raise RuntimeError("The running window could not accept more recordings")
                    return None
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Could not reach the running MCRAWsome window: {exc}") from None
                time.sleep(0.02)  # its bind may precede listen, or its process may be closing

    def attach(self, window: Any) -> None:
        self.window = window
        self.ready.set()

    def _receive(self) -> None:
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    if struct.unpack("3i", credentials)[1] != os.getuid():
                        continue
                    data = bytearray()
                    while not data.endswith(b"\n") and len(data) <= _LIMIT:
                        chunk = connection.recv(min(65536, _LIMIT + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if len(data) > _LIMIT:
                        continue
                    asked = json.loads(data)
                    paths = asked.get("paths") if isinstance(asked, dict) else None
                    view = asked.get("view") if isinstance(asked, dict) else None
                    if (
                        view not in (None, *VIEWS)
                        or not isinstance(paths, list)
                        or len(paths) > 256
                        or not all(
                            isinstance(path, str) and Path(path).is_absolute() and "\0" not in path
                            for path in paths
                        )
                    ):
                        continue
                    self.pending.put_nowait((paths, view))
                    connection.sendall(b"accepted\n")
                except (OSError, ValueError, queue.Full):
                    continue

    def _deliver(self) -> None:
        while not self.closed.is_set():
            if not self.ready.wait(0.2):
                continue
            if self.closed.is_set():
                return
            try:
                paths, view = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if paths:
                    self.window.evaluate_js(
                        "window.dispatchEvent(new CustomEvent('mcrawsome-open-files', {detail: "
                        + json.dumps(paths)
                        + "}));"
                    )
                if view is not None:
                    self.window.evaluate_js(f"window.location.hash = {json.dumps('#/' + view)};")
                self.window.restore()
                self.window.show()
            except Exception:
                log.exception("Could not deliver a desktop file-open request")

    def close(self) -> None:
        self.closed.set()
        self.ready.set()
        self.listener.close()
        self.receiver.join(timeout=1)
        self.delivery.join(timeout=1)
