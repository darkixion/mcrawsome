#!/usr/bin/python3 -I
"""Debian maintainer hook: stop identified MCRAWsome processes and restart engines as their users.

Standalone stdlib code because preinst runs before the new application is unpacked.
Never deletes recordings, settings, caches, mount records or user data.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

PENDING = Path("/run/mcrawsome-deb/engines.json")
NAME = "mcrawsome"
FUSE_TYPE = f" - fuse.{NAME} "
SERVICE_LAUNCHER = f"{NAME}-mount-service"
OVERRIDES = ("STATE_DIR", "CACHE_DIR", "MOUNT_DIR", "SERVER_TOKEN")
DESKTOP_ENVIRONMENT = {
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE",
    "XDG_SESSION_DESKTOP",
    "DESKTOP_SESSION",
    "GDK_BACKEND",
    "QT_QPA_PLATFORM",
}
ENVIRONMENT = {
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    *(f"MCRAWSOME_{name}" for name in OVERRIDES),
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
} | DESKTOP_ENVIRONMENT


def command(argv: list[str]) -> tuple[str, list[str]] | None:
    if argv and Path(argv[0]).name in {"systemd-run", "runuser"}:
        return None
    if "-m" in argv:
        index = argv.index("-m")
        module = argv[index + 1] if index + 1 < len(argv) else ""
        if module == f"{NAME}.cli.main":
            tail = argv[index + 2 :]
        elif module == f"{NAME}.mount.service":
            return "mount-service", argv[index + 2 :]
        else:
            return None
    elif argv and (
        Path(argv[0]).name == NAME
        or (Path(argv[0]).name.startswith(f"{NAME}-") and argv[0].endswith(".AppImage"))
    ):
        tail = argv[1:]
    elif len(argv) > 2 and Path(argv[1]).name == SERVICE_LAUNCHER:
        return "mount-service", argv[2:]
    elif len(argv) > 3 and argv[1] == "-I" and Path(argv[2]).name == SERVICE_LAUNCHER:
        return "mount-service", argv[3:]
    elif len(argv) > 1 and Path(argv[1]).name == NAME:
        tail = argv[2:]
    else:
        return None
    if tail and tail[0] in {"server", "gui", "mount-service"}:
        return tail[0], tail[1:]
    return None


def identity(pid: int) -> str | None:
    try:
        # comm can contain spaces and parentheses; fields after the last ')' start at state.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def processes() -> list[dict]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            argv = [part.decode() for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
            parsed = command(argv)
            if parsed is None:
                continue
            uid = entry.stat().st_uid
            if uid == 0:
                continue
            environment = dict(
                item.decode().split("=", 1)
                for item in (entry / "environ").read_bytes().split(b"\0")
                if b"=" in item
            )
            start = identity(int(entry.name))
            if start is not None:
                found.append(
                    {
                        "pid": int(entry.name),
                        "start": start,
                        "uid": uid,
                        "role": parsed[0],
                        "args": parsed[1],
                        "env": {k: v for k, v in environment.items() if k in ENVIRONMENT},
                        "cwd": str((entry / "cwd").resolve()),
                    }
                )
        except (OSError, ValueError, UnicodeError):
            continue
    return found


def configuration(engine: dict) -> dict:
    user = pwd.getpwuid(engine["uid"])
    folder = engine["env"].get("XDG_CONFIG_HOME", str(Path(user.pw_dir) / ".config"))
    try:
        return tomllib.loads((Path(folder) / NAME / "server.toml").read_text())
    except FileNotFoundError:
        return {}


def port(engine: dict) -> int:
    args = engine["args"]
    for index, arg in enumerate(args):
        if arg == "--port":
            return int(args[index + 1])
        if arg.startswith("--port="):
            return int(arg.split("=", 1)[1])
    return int(configuration(engine).get("port", 8642))


def request(engine: dict, path: str, post: bool = False) -> dict:
    token = engine["env"].get("MCRAWSOME_SERVER_TOKEN") or configuration(engine).get("token")
    for index, arg in enumerate(engine["args"]):
        if arg == "--token":
            token = engine["args"][index + 1]
        elif arg.startswith("--token="):
            token = arg.split("=", 1)[1]
    query = urllib.request.Request(
        f"http://127.0.0.1:{port(engine)}/api/v1{path}",
        method="POST" if post else "GET",
        data=b"{}" if post else None,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    # Local loopback traffic must not be redirected through a proxy from root's environment.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(query, timeout=30) as response:
        return json.load(response)


def pending() -> list[dict]:
    """The engines stopped for this replacement."""
    with contextlib.suppress(FileNotFoundError):
        engines: list[dict] = json.loads(PENDING.read_text())
        return engines
    return []


def forget() -> None:
    PENDING.unlink(missing_ok=True)


def save(engines: list[dict]) -> None:
    PENDING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PENDING.parent.is_symlink() or PENDING.parent.stat().st_uid != os.geteuid():
        raise RuntimeError("The package lifecycle state directory has an unexpected owner")
    PENDING.parent.chmod(0o700)
    temporary = PENDING.with_suffix(".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(engines, output)
    temporary.replace(PENDING)


def mounted(path: str) -> bool:
    escaped = path.replace("\\", r"\134").replace(" ", r"\040").replace("\t", r"\011").replace("\n", r"\012")
    return any(
        line.split()[4] == escaped and FUSE_TYPE in line
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
    )


def preserved_service(process: dict) -> dict | None:
    """An explicit handshake, not merely an old process that happens to still be alive."""
    try:
        folder = Path(process["args"][0])
        ready = json.loads((folder / "ready.json").read_text())
        runtime = ready["runtime"]
        root = Path(runtime["path"])
        manifest = json.loads((root / "runtime.json").read_text())
        argv = Path(f"/proc/{process['pid']}/cmdline").read_bytes().split(b"\0")
        if (
            ready["pid"] != process["pid"]
            or ready["start"] != process["start"]
            or identity(process["pid"]) != process["start"]
            or runtime["protocol"] != 1
            or manifest != {"id": root.name, "protocol": 1}
            or runtime["id"] != root.name
            or len(root.name) != 64
            or any(c not in "0123456789abcdef" for c in root.name)
            or os.fsencode(root / SERVICE_LAUNCHER) not in argv
        ):
            return None
        cgroup = Path(f"/proc/{process['pid']}/cgroup").read_text()
        if not re.search(r"/mcrawsome-mount-[0-9a-f]{32}\.service(?:\n|$)", cgroup):
            return None  # Revision9 pinned code, but still inherited the installer's cgroup.
        record = json.loads((folder / "sequence.json").read_text())
        return {"id": record["id"], "pid": process["pid"], "start": process["start"], "uid": process["uid"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def stop(remember: bool, preserve_mounts: bool = False) -> None:
    running = processes()
    preserved = {}
    if preserve_mounts:
        for process in running:
            if process["role"] != "mount-service" or identity(process["pid"]) != process["start"]:
                continue
            record = json.loads((Path(process["args"][0]) / "sequence.json").read_text())
            if not mounted(record["path"]):
                continue
            service = preserved_service(process)
            if service is None:
                # One-time transition from pre-isolation services. Only normal unmount is safe.
                user = pwd.getpwuid(process["uid"])
                subprocess.run(
                    [
                        "/usr/sbin/runuser",
                        "-u",
                        user.pw_name,
                        "--",
                        "/usr/bin/fusermount3",
                        "-u",
                        record["path"],
                    ],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                if mounted(record["path"]):
                    raise RuntimeError(
                        "This media predates independent mount services. Save your Resolve project and "
                        "close Resolve, then retry. Future upgrades can keep mounts open."
                    )
                continue
            preserved[process["pid"]] = service
    engines = []
    for process in running:
        if process["role"] != "server":
            continue
        try:
            health = request(process, "/health")
        except OSError:
            continue  # A wrapper, a server still starting, or one already stopping.
        if health.get("pid") != process["pid"]:
            continue
        process = dict(process, env=dict(process["env"]))
        # A version with an incomplete restart environment may already have lost X authority.
        # A window on this user's same engine supplies its current desktop session, not media scope.
        for window in running:
            if window["role"] == "gui" and window["uid"] == process["uid"] and port(window) == port(process):
                process["env"].update({k: v for k, v in window["env"].items() if k in DESKTOP_ENVIRONMENT})
        engines.append(process)
        # Use the engine's normal unmount operation, never a lazy/forced unmount.
        if preserve_mounts:
            active = [item for item in request(process, "/mounts").get("mounts", []) if item["active"]]
            owned = [item for item in preserved.values() if item["uid"] == process["uid"]]
            if any(item["id"] not in {service["id"] for service in owned} for item in active):
                raise RuntimeError(
                    "Some mounted media cannot be adopted safely. Unmount it before upgrading."
                )
            process["preserved_mounts"] = [
                item for item in owned if item["id"] in {mount["id"] for mount in active}
            ]
        elif health.get("active_mounts", 0):
            request(process, "/mounts/unmount-all", True)
            if request(process, "/health").get("active_mounts", 0):
                raise RuntimeError(
                    "Could not unmount MCRAWsome media. Close applications using it and retry."
                )
    for process in running:
        if (
            preserve_mounts
            or process["role"] != "mount-service"
            or identity(process["pid"]) != process["start"]
        ):
            continue
        folder = Path(process["args"][0])
        record = json.loads((folder / "sequence.json").read_text())
        if not mounted(record["path"]):
            continue
        user = pwd.getpwuid(process["uid"])
        result = subprocess.run(
            ["/usr/sbin/runuser", "-u", user.pw_name, "--", "/usr/bin/fusermount3", "-u", record["path"]],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode and mounted(record["path"]):
            raise RuntimeError("Could not unmount MCRAWsome media; close applications using it and retry.")
    if remember:
        combined = {(item["uid"], port(item)): item for item in pending() + engines}
        save(list(combined.values()))
    else:
        forget()
    # Windows are closed first so they cannot reconnect or restart the engine during replacement.
    for process in sorted(running, key=lambda item: item["role"] == "server"):
        if process["role"] == "mount-service":
            continue  # normal unmount makes the filesystem process exit itself
        if identity(process["pid"]) != process["start"]:
            continue
        try:
            descriptor = os.pidfd_open(process["pid"])
            try:
                if identity(process["pid"]) == process["start"]:
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            finally:
                os.close(descriptor)
        except ProcessLookupError:
            pass
    waiting = [item for item in running if item["pid"] not in preserved]
    deadline = time.monotonic() + 60
    while any(identity(item["pid"]) == item["start"] for item in waiting):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "MCRAWsome has not finished stopping; package replacement was halted. Retry shortly."
            )
        time.sleep(0.2)


SPAWN = """
import os, pathlib, subprocess, sys
from mcrawsome.paths import state_dir
from mcrawsome.processes import spawn
folder = state_dir()
folder.mkdir(parents=True, exist_ok=True)
with (folder / 'server.log').open('ab') as output:
    spawn(['/usr/bin/mcrawsome', 'server', *sys.argv[1:]], role='engine', stdin=subprocess.DEVNULL,
                     stdout=output, stderr=output, start_new_session=True)
"""


def desktop_environment(engine: dict, user: pwd.struct_passwd) -> dict:
    environment = dict(
        engine["env"],
        HOME=user.pw_dir,
        USER=user.pw_name,
        LOGNAME=user.pw_name,
        PATH="/usr/local/bin:/usr/bin:/bin",
    )
    if environment.get("DISPLAY") and not environment.get("XAUTHORITY"):
        # Older packages dropped XAUTHORITY. If no MCRAWsome window is open during replacement,
        # recover only desktop keys from this user's session manager, running as that user.
        try:
            session = subprocess.run(
                [
                    "/usr/sbin/runuser",
                    "-u",
                    user.pw_name,
                    "--",
                    "/usr/bin/systemctl",
                    "--user",
                    "show-environment",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if session.returncode == 0:
                for line in session.stdout.splitlines():
                    key, separator, value = line.partition("=")
                    if separator and key in DESKTOP_ENVIRONMENT:
                        environment[key] = value
        except (OSError, subprocess.TimeoutExpired):
            pass  # Keep the recorded session; headless engines need no desktop credentials.
    return environment


def restart() -> None:
    for engine in pending():
        user = pwd.getpwuid(engine["uid"])
        environment = desktop_environment(engine, user)
        subprocess.run(
            [
                "/usr/sbin/runuser",
                "-u",
                user.pw_name,
                "--",
                "/usr/bin/python3",
                "-I",
                "-c",
                SPAWN,
                *engine["args"],
            ],
            env=environment,
            cwd=engine["cwd"] if Path(engine["cwd"]).is_dir() else user.pw_dir,
            check=True,
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                if request(engine, "/health").get("pid"):
                    # A preserved service that has exited (its media ejected, say) has unmounted it, so
                    # there is nothing left to adopt; the engine mounts sent media again itself.
                    expected = [
                        item
                        for item in engine.get("preserved_mounts", [])
                        if identity(item["pid"]) == item["start"]
                    ]
                    if expected:
                        active = {
                            item["id"]
                            for item in request(engine, "/mounts").get("mounts", [])
                            if item["active"] and item["serving"]
                        }
                        if any(item["id"] not in active for item in expected):
                            raise RuntimeError(
                                "The new engine did not adopt all preserved mounts; old services were "
                                "left running. Close Resolve and eject that media, then run "
                                "sudo dpkg --configure -a."
                            )
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"The new MCRAWsome engine on port {port(engine)} did not start; see its server.log"
                )
            time.sleep(0.2)
    forget()


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Package lifecycle hooks must run as root")
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action in {"install", "upgrade", "failed-upgrade"}:
        stop(remember=True, preserve_mounts=True)
    elif action in {"remove", "deconfigure"}:
        stop(remember=False)
    elif action in {"configure", "abort-upgrade", "abort-remove", "abort-deconfigure"}:
        restart()


if __name__ == "__main__":
    main()
