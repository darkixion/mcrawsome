"""Long-lived desktop processes belong to independent user units, never the installer's cgroup."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from mcrawsome.appimage import host_environment, launcher

Role = Literal["engine", "mount", "resolve", "window"]
_MANAGER_ENV = {
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "SYSTEMD_EXEC_PID",
    "NOTIFY_SOCKET",
    "WATCHDOG_PID",
    "WATCHDOG_USEC",
    "LISTEN_FDS",
    "LISTEN_PID",
    "LISTEN_FDNAMES",
    "SERVICE_RESULT",
    "EXIT_CODE",
    "EXIT_STATUS",
}


def systemd_available() -> bool:
    if sys.platform != "linux":
        return False
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return (
        sys.platform == "linux"
        and Path("/usr/bin/systemd-run").is_file()
        and (Path(runtime) / "bus").exists()
    )


def requires_systemd() -> bool:
    return Path(__file__).with_name("MOUNT_RUNTIME").is_file()


def process_id(process: subprocess.Popen[bytes]) -> int:
    return int(getattr(process, "mcrawsome_pid", process.pid))


def stop_unit(process: subprocess.Popen[bytes], sig: signal.Signals = signal.SIGTERM) -> bool:
    unit = getattr(process, "mcrawsome_unit", None)
    if unit is None:
        return False
    if sig == signal.SIGTERM:
        command = ["/usr/bin/systemctl", "--user", "--no-block", "stop", unit]
    else:
        command = ["/usr/bin/systemctl", "--user", "kill", "--kill-whom=all", f"--signal={int(sig)}", unit]
    subprocess.run(command, check=False, capture_output=True, timeout=5, env=host_environment())
    return True


def spawn(command: list[str], *, role: Role, **options: Any) -> subprocess.Popen[bytes]:
    """Start `command` as its own user service. Inside the AppImage it gets the host's environment: a host
    program (xdg-open) must not load the image's libraries, and the image itself sets them up again."""
    if launcher() is not None:
        options.setdefault("env", host_environment())
    if not systemd_available():
        if requires_systemd():
            raise OSError("MCRAWsome needs the desktop user's systemd session to start independent services")
        return subprocess.Popen(command, **options)
    unit = f"mcrawsome-{role}-{uuid.uuid4().hex}.service"
    environment = dict(options.get("env") or os.environ)
    environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={environment['XDG_RUNTIME_DIR']}/bus")
    options["env"] = environment
    managed = [
        "/usr/bin/systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--wait",
        "--pipe",
        "--service-type=exec",
        "--expand-environment=no",
        f"--unit={unit}",
        f"--working-directory={options.get('cwd') or os.getcwd()}",
    ]
    if role == "resolve":
        # xdg-open may fork its real application and then exit; retain all of its descendants.
        managed.append("--property=ExitType=cgroup")
    for name in sorted(environment):
        if name not in _MANAGER_ENV and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            managed += ["--setenv", name]  # Values travel over D-Bus, not in the process command line.
    managed += ["--", *command]
    process = subprocess.Popen(managed, **options)
    vars(process)["mcrawsome_unit"] = unit
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=MainPID",
                    "--property=ActiveState",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                env=host_environment(),
            )
            properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            pid = int(properties.get("MainPID", "0"))
            if pid:
                vars(process)["mcrawsome_pid"] = pid
                return process
            if role == "resolve" and properties.get("ActiveState") == "active":
                return process
            if process.poll() is not None:
                if process.returncode:
                    raise OSError(
                        f"Could not start independent MCRAWsome {role} service (status {process.returncode})"
                    )
                return process  # A short-lived desktop handler completed successfully.
            time.sleep(0.02)
        raise OSError(f"The independent MCRAWsome {role} service did not start")
    except BaseException:
        stop_unit(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
