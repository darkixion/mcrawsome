"""The preview measured through the API.

    uv run scripts/bench_preview.py CLIP [--width 1920]

It starts its own `mcrawsome server` (on a free port, with isolated state under SCRATCH) and, over
the preview WebSocket, measures: opening and the first frame (cold seek), 20 random frames (warm seek), 50
exposure changes one at a time (slider to frame: request sent to JPEG received and decoded), the refined
frame, cancelling (a request overtaken by a newer one must never be answered), the frame before the display
table as 16-bit data, and the peak memory of the server and preview process. Prints a table, and
everything as JSON with --json.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from websockets.sync.client import ClientConnection, connect

AS_LINEAR16 = {"as": "linear16"}  # the reply: the frame before the display table, not a JPEG
SCRATCH = Path.home() / ".cache" / "mcrawsome-spike"
API = "/api/v1"


def load() -> float:
    return os.getloadavg()[0]


def stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(values),
        "median": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(np.ceil(0.95 * len(ordered))) - 1)], 1),
        "max": round(ordered[-1], 1),
    }


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def peak_kib(pid: int) -> int:
    """The process's peak resident memory (VmHWM) in KiB, 0 once it has gone."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def children(pid: int) -> list[int]:
    """Every descendant of `pid`."""
    parents: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            parents.setdefault(int(fields[1]), []).append(int(entry.name))
    found, todo = [], [pid]
    while todo:
        for child in parents.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def rss_kib(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
        except OSError:
            pass
    return total


class Sampler(threading.Thread):
    """The largest total resident memory of a process tree, sampled every 0.25 s."""

    def __init__(self, pid: int) -> None:
        super().__init__(daemon=True)
        self.pid, self.peak, self.stop = pid, 0, threading.Event()

    def run(self) -> None:
        while not self.stop.wait(0.25):
            self.peak = max(self.peak, rss_kib([self.pid, *children(self.pid)]))


class Preview:
    """The preview WebSocket: requests with sequence numbers, replies as (header, payload)."""

    def __init__(self, port: int, clip: Path, width: int) -> None:
        path = urllib.parse.quote(str(clip))
        url = f"ws://127.0.0.1:{port}{API}/clips/local/preview?path={path}&width={width}"
        self._stack = contextlib.ExitStack()
        opened = connect(url, max_size=None, open_timeout=120, compression=None)
        self.ws: ClientConnection = self._stack.enter_context(opened)
        self.opened = self.reply()[0]
        if self.opened["kind"] != "opened":
            raise SystemExit(f"the preview did not open: {self.opened}")
        self.seq = 0

    def send(self, **request: Any) -> int:
        self.seq += 1
        self.ws.send(json.dumps({"seq": self.seq, **request}))
        return self.seq

    def reply(self, timeout: float = 300) -> tuple[dict[str, Any], bytes]:
        header = json.loads(self.ws.recv(timeout))
        payload = self.ws.recv(timeout) if header.get("bytes") else b""
        assert isinstance(payload, bytes)
        return header, payload

    def until(self, seq: int, kind: str) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
        """Replies until `kind` for `seq`; the others seen on the way are returned too."""
        seen: list[dict[str, Any]] = []
        while True:
            header, payload = self.reply()
            if header["kind"] == "error":
                raise SystemExit(f"the preview failed: {header['error']}")
            if header.get("seq") == seq and header["kind"] == kind:
                return header, payload, seen
            seen.append(header)

    def timed(self, kind: str = "draft", **request: Any) -> tuple[float, dict[str, Any], bytes]:
        start = time.perf_counter()
        header, payload, _ = self.until(self.send(**request), kind)
        if kind in ("draft", "refined"):
            cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)  # as a page would show it
        return (time.perf_counter() - start) * 1000, header, payload

    def close(self) -> None:
        self._stack.close()


def measure(preview: Preview, frames: int, rng: random.Random) -> dict[str, Any]:
    out: dict[str, Any] = {}
    middle = frames // 2
    recipe = {"develop": {"exposure": 0.0}}
    out["cold_seek_ms"] = round(preview.timed(op="recipe", index=middle, recipe=recipe)[0], 1)
    warm = [preview.timed(op="frame", index=rng.randrange(frames))[0] for _ in range(20)]
    out["warm_seek_ms"] = stats(warm)
    preview.timed(op="frame", index=middle)
    slider, engine = [], []
    for i in range(50):
        stops = round(np.sin(i * 0.7) * 1.5, 2) + (0.01 if i % 2 else 0.0)
        ms, header, _ = preview.timed(op="recipe", recipe={"develop": {"exposure": stops}})
        slider.append(ms)
        engine.append(header["ms"])
    out["slider_to_frame_ms"] = stats(slider)
    out["slider_engine_ms"] = stats(engine)
    later = [min(frames - 1, middle + step) for step in (0, 1, 2)]
    refined = [preview.timed("refined", op="refine", index=i)[0] for i in later]
    out["refined_first_ms"] = round(refined[0], 1)  # plans the motion once per session
    out["refined_ms"] = stats(refined[1:])
    overtaken = 0
    for _ in range(10):
        first = preview.send(op="frame", index=rng.randrange(frames))
        second = preview.send(op="recipe", recipe={"develop": {"exposure": rng.uniform(-1, 1)}})
        _, _, seen = preview.until(second, "draft")
        overtaken += sum(1 for header in seen if header.get("seq") == first)
    out["cancelling"] = {"trials": 10, "overtaken_replies_delivered": overtaken}
    linear, sizes = [], set()
    for i in range(20):
        stops = round(np.sin(i * 0.9) * 1.5, 2)
        ms, header, payload = preview.timed(
            "linear16",
            op="recipe",
            recipe={"develop": {"exposure": stops}},
            **AS_LINEAR16,
        )
        linear.append(ms)
        sizes.add(len(payload))
    out["linear16_exposure_change_ms"] = stats(linear)
    out["linear16_bytes"] = sorted(sizes)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("clip", type=Path)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    clip = args.clip.resolve()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    port = free_port()
    roots = {clip.parent, SCRATCH}
    command = [sys.executable, "-m", "mcrawsome.cli.main", "server", "--port", str(port)]
    for root in sorted(roots):
        command += ["--library", str(root)]
    environment = {**os.environ, "MCRAWSOME_STATE_DIR": str(SCRATCH / "state")}  # isolated state
    server = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, start_new_session=True)
    results: dict[str, Any] = {
        "clip": clip.name,
        "width": args.width,
        "load_start": load(),
    }
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{API}/health", timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise SystemExit("the server did not start") from None
                time.sleep(0.2)
        start = time.perf_counter()
        preview = Preview(port, clip, args.width)
        results["open_ms"] = round((time.perf_counter() - start) * 1000, 1)
        results["preview_size"] = [preview.opened["width"], preview.opened["height"]]
        results |= measure(preview, preview.opened["frame_count"], random.Random(args.seed))
        previewing = children(server.pid)
        results["memory_mib"] = {
            "server_peak": round(peak_kib(server.pid) / 1024),
            "preview_process_peak": round(max((peak_kib(pid) for pid in previewing), default=0) / 1024),
        }
        preview.close()
        results["load_end"] = load()
    finally:
        server.terminate()
        server.wait(30)
    if args.json:
        print(json.dumps(results, indent=2))
        return
    for key, value in results.items():
        print(f"{key:32} {json.dumps(value)}")


if __name__ == "__main__":
    main()
