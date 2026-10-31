"""Start a real desktop window and reject any CRITICAL output, including the detached engine's log.

Run with IMAGE --output DIR [--clip CLIP], or --source --output DIR for the source CLI.
The clip's parent must contain only samples approved for the smoke test.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.util
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def critical_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if "CRITICAL" in line]


def processes(roots: set[int]) -> dict[int, tuple[str, list[str]]]:
    records: dict[int, tuple[int, str]] = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            parent = int((path / "stat").read_text().rsplit(")", 1)[1].split()[1])
            command = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            records[int(path.name)] = (parent, command)
        except (OSError, ValueError):
            continue
    selected = set(roots)
    while True:
        found = selected | {pid for pid, (parent, _) in records.items() if parent in selected}
        if found == selected:
            break
        selected = found
    result = {}
    for pid in selected & records.keys():
        libraries: list[str] = []
        with contextlib.suppress(OSError):
            maps = Path(f"/proc/{pid}/maps").read_text().splitlines()
            libraries = sorted(
                {
                    line.split()[-1]
                    for line in maps
                    if any(
                        name in line
                        for name in ("libgio", "libglib", "libgobject", "libsoup", "/gio/modules/")
                    )
                }
            )
        result[pid] = (records[pid][1], libraries)
    return result


def merge_processes(
    earlier: dict[int, tuple[str, list[str]]], later: dict[int, tuple[str, list[str]]]
) -> dict[int, tuple[str, list[str]]]:
    """Keep maps seen while a process lived, even when its final snapshot is unreadable."""
    merged = dict(earlier)
    for pid, (command, libraries) in later.items():
        previous = earlier.get(pid, (command, []))[1]
        merged[pid] = command, sorted(set(previous) | set(libraries))
    return merged


def close_window(pids: set[int]) -> bool:
    """Send WM_DELETE_WINDOW to this run's X11 window, just as its close button does."""
    x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
    pointer, window = ctypes.c_void_p, ctypes.c_ulong
    x11.XOpenDisplay.restype = pointer
    x11.XDefaultRootWindow.argtypes = [pointer]
    x11.XDefaultRootWindow.restype = window
    x11.XInternAtom.argtypes = [pointer, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = window
    x11.XQueryTree.argtypes = [
        pointer,
        window,
        ctypes.POINTER(window),
        ctypes.POINTER(window),
        ctypes.POINTER(ctypes.POINTER(window)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XGetWindowProperty.argtypes = [
        pointer,
        window,
        window,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_int,
        window,
        ctypes.POINTER(window),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(window),
        ctypes.POINTER(window),
        ctypes.POINTER(pointer),
    ]
    x11.XFree.argtypes = [pointer]
    x11.XCloseDisplay.argtypes = [pointer]
    x11.XFlush.argtypes = [pointer]
    x11.XSendEvent.argtypes = [pointer, window, ctypes.c_int, ctypes.c_long, pointer]

    class Message(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_int),
            ("serial", window),
            ("send_event", ctypes.c_int),
            ("display", pointer),
            ("window", window),
            ("message_type", window),
            ("format", ctypes.c_int),
            ("data", ctypes.c_long * 5),
        ]

    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        pid_atom = x11.XInternAtom(display, b"_NET_WM_PID", 0)
        pending = [x11.XDefaultRootWindow(display)]
        while pending:
            target = pending.pop()
            root, parent, children, count = window(), window(), ctypes.POINTER(window)(), ctypes.c_uint()
            if x11.XQueryTree(
                display,
                target,
                ctypes.byref(root),
                ctypes.byref(parent),
                ctypes.byref(children),
                ctypes.byref(count),
            ):
                pending.extend(children[i] for i in range(count.value))
                if children:
                    x11.XFree(children)
            kind, size, items, after, data = window(), ctypes.c_int(), window(), window(), pointer()
            x11.XGetWindowProperty(
                display,
                target,
                pid_atom,
                0,
                1,
                0,
                6,
                ctypes.byref(kind),
                ctypes.byref(size),
                ctypes.byref(items),
                ctypes.byref(after),
                ctypes.byref(data),
            )
            pid = ctypes.cast(data, ctypes.POINTER(window))[0] if data and items.value else None
            if data:
                x11.XFree(data)
            if pid not in pids:
                continue
            message = Message()
            message.type, message.send_event, message.display = 33, 1, display
            message.window, message.format = target, 32
            message.message_type = x11.XInternAtom(display, b"WM_PROTOCOLS", 0)
            message.data[0] = x11.XInternAtom(display, b"WM_DELETE_WINDOW", 0)
            # XSendEvent reads a complete XEvent union (24 longs).
            event = (ctypes.c_long * 24)()
            ctypes.memmove(event, ctypes.byref(message), ctypes.sizeof(message))
            x11.XSendEvent(display, target, 0, 0, ctypes.byref(event))
            x11.XFlush(display)
            return True
        return False
    finally:
        x11.XCloseDisplay(display)


def declined_offer(image: Path, data: Path) -> str:
    """The settings line that records the menu-entry offer as declined for `image` with menu data in `data`,
    so the run shows no dialog."""
    saved = {name: os.environ.get(name) for name in ("MCRAWSOME_APPIMAGE", "XDG_DATA_HOME")}
    os.environ.update(MCRAWSOME_APPIMAGE=str(image), XDG_DATA_HOME=str(data))
    try:
        from mcrawsome.desktop import desktop_entry_status

        status = desktop_entry_status()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return f'desktop_entry_offer = "{status[0]}:declined"\n' if status else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, nargs="?")
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--clip", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not args.source and args.image is None:
        parser.error("supply an AppImage or --source")
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("tmp", "config/mcrawsome", "state", "cache", "data"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    offer = "" if args.image is None else declined_offer(args.image.resolve(), folder / "data")
    (folder / "config/mcrawsome/interface.toml").write_text(f"workers = 2\n{offer}")
    env = dict(
        os.environ,
        TMPDIR=str(folder / "tmp"),
        GDK_BACKEND="x11",
        XDG_CONFIG_HOME=str(folder / "config"),
        XDG_DATA_HOME=str(folder / "data"),  # the menu entry and file types the AppImage offers to add
        MCRAWSOME_STATE_DIR=str(folder / "state"),
        MCRAWSOME_CACHE_DIR=str(folder / "cache"),
    )
    env.pop("MCRAWSOME_SERVER_TOKEN", None)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    command = ["uv", "run", "--extra", "gui", "mcrawsome"] if args.source else [str(args.image.resolve())]
    command += ["gui", "--port", str(port)]
    if args.clip:
        command.append(str(args.clip.resolve()))
    url = f"http://127.0.0.1:{port}/api/v1"
    ready = False
    closed = False
    records: dict[int, tuple[str, list[str]]] = {}
    roots: set[int] = set()
    with (folder / "window.log").open("w") as output:
        process = subprocess.Popen(
            command, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        roots.add(process.pid)
        try:
            deadline = time.monotonic() + args.timeout
            shown: float | None = None
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"{url}/health", timeout=0.5) as response:
                        health: dict[str, Any] = json.load(response)
                    pid = int(health["pid"])
                    environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                    if f"MCRAWSOME_STATE_DIR={folder / 'state'}".encode() in environment:
                        roots.add(pid)
                except (OSError, ValueError, KeyError, urllib.error.URLError):
                    pass
                current = processes(roots)
                records = merge_processes(records, current)
                if len(roots) > 1 and any("WebKitWebProcess" in name for name, _ in current.values()):
                    if shown is None:
                        shown = time.monotonic()
                    if time.monotonic() - shown >= args.duration:
                        ready = True
                        break
                else:
                    shown = None
                time.sleep(0.1)
            if ready and close_window(set(records)):
                deadline = time.monotonic() + args.timeout
                while process.poll() is None and time.monotonic() < deadline:
                    records = merge_processes(records, processes(roots))
                    time.sleep(0.1)
                closed = process.poll() == 0
        finally:
            # Stop only this smoke run's process tree, including the engine's separate session.
            current = processes(roots)
            records = merge_processes(records, current)
            for pid in roots & current.keys():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGSTOP)
            for pid in sorted(current, reverse=True):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            process.wait(timeout=5)
    logs = (folder / "window.log").read_text()
    server_log = folder / "state/server.log"
    if server_log.exists():
        logs += server_log.read_text()
    criticals = critical_lines(logs)
    result = {"window_started": ready, "window_closed": closed, "criticals": criticals, "processes": records}
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {"window_started": ready, "window_closed": closed, "criticals": criticals, "output": str(folder)},
            indent=2,
        )
    )
    return 0 if ready and closed and not criticals else 1


if __name__ == "__main__":
    raise SystemExit(main())
