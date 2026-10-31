"""Run one browser at a time and sample its process tree and the engine on Linux.

Use --appdir with an extracted MCRAWsome AppImage. AppRun's environment is preserved; only its
last Python command is replaced with the isolated window harness. Never changes the AppImage.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def snapshot(root: int) -> dict[str, Any]:
    processes: dict[int, tuple[int, int, int]] = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            processes[int(path.name)] = (int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[21]))
        except (OSError, ValueError, IndexError):
            continue
    selected = {root}
    while True:
        children = {pid for pid, (parent, _, _) in processes.items() if parent in selected}
        if children <= selected:
            break
        selected |= children
    ticks, pages = 0, 0
    clients: dict[str, dict[str, int]] = {}
    drivers: set[str] = set()
    for pid in selected:
        if pid not in processes:
            continue
        _, cpu, rss = processes[pid]
        ticks += cpu
        pages += rss
        try:
            for fd in (Path("/proc") / str(pid) / "fdinfo").iterdir():
                try:
                    info = dict(line.split(":", 1) for line in fd.read_text().splitlines() if ":" in line)
                    if "drm-client-id" not in info:
                        continue
                    drivers.add(info.get("drm-driver", "unknown").strip())
                    clients[info["drm-client-id"].strip()] = {
                        key: int(value.strip().split()[0])
                        for key, value in info.items()
                        if key.startswith("drm-engine-") and "capacity" not in key
                    }
                except (OSError, ValueError):
                    continue
        except OSError:
            continue
    engines: dict[str, int] = {}
    for client in clients.values():
        for key, value in client.items():
            engines[key] = engines.get(key, 0) + value
    return {
        "cpu_s": ticks / os.sysconf("SC_CLK_TCK"),
        "rss_mib": pages * os.sysconf("SC_PAGE_SIZE") / 2**20,
        "engines_ns": engines,
        "clients": list(clients),
        "drivers": sorted(drivers),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--browser", choices=["chromium", "webkit"], required=True)
    parser.add_argument("--appdir", type=Path)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--port", type=int, default=18861)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    result_file = output / f"{args.browser}.json"
    result_file.unlink(missing_ok=True)
    (output / "phase.json").unlink(missing_ok=True)
    server_log = (output / f"{args.browser}-server.log").open("w")
    browser_log = (output / f"{args.browser}.log").open("w")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scripts.gpu_trial.bench",
            str(args.clip),
            "--output",
            str(output),
            "--port",
            str(args.port),
            "--frames",
            str(args.frames),
        ],
        stdout=server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=HERE.parents[1],
    )
    browser: subprocess.Popen[bytes] | None = None
    samples = []
    try:
        for _ in range(300):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/meta", timeout=1) as response:
                    json.load(response)
                break
            except (urllib.error.URLError, TimeoutError):
                if server.poll() is not None:
                    raise RuntimeError("Server exited; see its log") from None
                time.sleep(0.1)
        else:
            raise RuntimeError("Server did not start")
        url = f"http://127.0.0.1:{args.port}/?browser={args.browser}"
        if args.browser == "chromium":
            command = [
                "chromium",
                f"--user-data-dir={output / 'chromium-profile'}",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--disable-features=MediaRouter,OptimizationHints",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--ozone-platform=x11",
                "--use-gl=angle",
                "--use-angle=gl",
                url,
            ]
        else:
            if args.appdir is None:
                raise ValueError("--appdir is needed for the bundled WebKit window")
            appdir = args.appdir.resolve()
            source = (appdir / "AppRun").read_text()
            source = (
                "\n".join(
                    "here=" + shlex.quote(str(appdir)) if line.startswith("here=") else line
                    for line in source.splitlines()
                )
                + "\n"
            )
            old = '"$APPDIR/usr/python/bin/python3" -m mcrawsome.cli.main "$@"'
            if old not in source:
                raise RuntimeError("AppRun entry point changed")
            source = source.replace(
                old, '"$APPDIR/usr/python/bin/python3" ' + shlex.quote(str(HERE / "window.py")) + ' "$@"'
            )
            launcher = output / "run-webkit.sh"
            launcher.write_text(source)
            command = ["bash", str(launcher), "--output", str(output), "--port", str(args.port)]
        browser = subprocess.Popen(
            command, stdout=browser_log, stderr=subprocess.STDOUT, start_new_session=True
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            phase_file = output / "phase.json"
            try:
                phase = json.loads(phase_file.read_text())["name"]
            except (OSError, json.JSONDecodeError):
                phase = "startup"
            samples.append(
                {
                    "time": time.monotonic(),
                    "phase": phase,
                    "server": snapshot(server.pid),
                    "browser": snapshot(browser.pid),
                }
            )
            if result_file.exists():
                break
            if browser.poll() is not None:
                raise RuntimeError("Browser exited without a result; see its log")
            time.sleep(0.1)
        else:
            raise RuntimeError("Browser did not complete in five minutes")
        if args.browser == "webkit":
            browser.wait(timeout=15)  # let the PNG snapshot finish before stopping its process tree
        (output / f"{args.browser}-resources.json").write_text(
            json.dumps({"load_after": os.getloadavg(), "samples": samples}, indent=2) + "\n"
        )
        result = json.loads(result_file.read_text())
        print(
            json.dumps(
                {
                    "browser": args.browser,
                    "renderer": result.get("renderer"),
                    "error": result.get("error"),
                    "fps": {key: round(value["fps"], 2) for key, value in result["runs"].items()},
                },
                indent=2,
            )
        )
        if result.get("error"):
            raise RuntimeError(result["error"])
    finally:
        for process in (browser, server):
            if process is not None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        server_log.close()
        browser_log.close()


if __name__ == "__main__":
    main()
