"""Local-only benchmark server. Run with uv run python -m scripts.gpu_trial.bench.

Every requested frame is freshly decoded. Only the explicitly approved clip can be opened.
The HTML page exercises compute/JPEG, CPU/JPEG and reduced Bayer/WebGL through the same HTTP server.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
from websockets.sync.server import ServerConnection, serve

from mcrawsome import _raw
from mcrawsome.colour.display import display_table
from mcrawsome.colour.transfer import acescct_encode32
from mcrawsome.develop.raw import Developer
from mcrawsome.io.clip import Clip
from mcrawsome.preview.finish import display_bgr

from .common import approved_clip, difference, frame_order, reduce_bayer, reduce_plane
from .compute import Compute

HERE = Path(__file__).parent


class Trial:
    def __init__(self, clip: Path, output: Path, frames: int, width: int) -> None:
        self.clip = Clip(approved_clip(clip))
        self.output, self.frames, self.width = output, frames, width
        self.output.mkdir(parents=True, exist_ok=True)
        first = self.clip.raw_frame(0)
        self.developer = Developer(self.clip.metadata, first.metadata, first.metadata.as_shot_neutral)
        self.table = display_table(33)
        dev = self.developer
        self.gpu = Compute(dev.black, dev.gain, dev.matrix, self.table, dev.cfa, dev.rotate_180)
        h, w = first.data.shape
        self.meta = {
            "raw_width": w // 2,
            "raw_height": h // 2,
            "width": width,
            "height": round(width * h / w),
            "cfa": ["rgb".index(c) for c in dev.cfa],
            "rotate": dev.rotate_180,
            "matrix": dev.matrix.ravel().tolist(),
            "size": 33,
            "frames": frames,
            "count": min(60, self.clip.frame_count),
            "device": self.gpu.device_name,
            "driver": self.gpu.driver,
            "source_shape": [h, w],
            "white": dev.white,
            "exposure": 0.0,
            "neutral": first.metadata.as_shot_neutral,
        }
        self.calibration = np.stack((reduce_plane(dev.black), reduce_plane(dev.gain)), -1).astype("<f4")
        self.cpu_start = time.process_time()
        self.start = time.perf_counter()
        self.request_times: list[dict[str, Any]] = []

    def frame(self, index: int, route: str, method: str) -> tuple[bytes, dict[str, float], str]:
        started = time.perf_counter()
        raw = self.clip.raw_frame(index).data
        decoded = time.perf_counter()
        timings = {"decode_ms": (decoded - started) * 1000}
        if route == "raw":
            reduced = reduce_bayer(raw)
            prepared = time.perf_counter()
            payload = reduced.astype("<u2", copy=False).tobytes()
            timings.update(
                reduce_ms=(prepared - decoded) * 1000, pack_ms=(time.perf_counter() - prepared) * 1000
            )
            mime = "application/octet-stream"
        else:
            if route == "compute":
                rgb, gpu_times = self.gpu.render(raw, method, self.width, 0.0)
                timings.update(gpu_times)
                bgr = np.ascontiguousarray(rgb[..., ::-1])
            else:
                dev = self.developer
                step = 4 if raw.shape[1] // 2 >= 2 * self.width else 2
                linear = dev.develop_fast(raw, step, self.width)
                if linear.shape[1] > self.width:
                    linear = np.asarray(
                        cv2.resize(
                            linear,
                            (self.width, round(self.width * linear.shape[0] / linear.shape[1])),
                            interpolation=cv2.INTER_AREA,
                        ),
                        dtype=np.float32,
                    )
                bgr = _raw.apply_preview(linear, self.table, 0.0)
                timings["develop_ms"] = (time.perf_counter() - decoded) * 1000
            encode = time.perf_counter()
            ok, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                raise RuntimeError("JPEG encode failed")
            payload = jpeg.tobytes()
            timings["encode_ms"] = (time.perf_counter() - encode) * 1000
            mime = "image/jpeg"
        timings["server_ms"] = (time.perf_counter() - started) * 1000
        timings["bytes"] = len(payload)
        self.request_times.append({"route": route, "method": method, "index": index, **timings})
        return payload, timings, mime

    def reference(self, index: int, recovery: bool = True) -> np.ndarray[Any, np.dtype[np.uint8]]:
        raw = self.clip.raw_frame(index).data
        dev = self.developer
        original = dev.highlight_recovery
        try:
            dev.highlight_recovery = 1.0 if recovery else 0.0
            # Full Malvar development, full highlight recovery, no temporal or film effects.
            linear = dev.develop(raw)
        finally:
            dev.highlight_recovery = original
        encoded = acescct_encode32(linear)
        encoded = np.asarray(
            cv2.resize(encoded, (self.meta["width"], self.meta["height"]), interpolation=cv2.INTER_AREA),
            dtype=np.float32,
        )
        return np.ascontiguousarray(display_bgr(encoded, self.table)[..., ::-1])

    def engine_bench(self) -> None:
        result: dict[str, Any] = {"metadata": self.meta, "load_before": os.getloadavg()}
        # Warm compilation, buffers and file cache before the timed passes.
        for method in ("quad", "bilinear"):
            self.frame(0, "compute", method)
        for route, method in (("cpu", "quad"), ("compute", "quad"), ("compute", "bilinear")):
            for mode in ("playback", "scrub"):
                rows = []
                wall, cpu = time.perf_counter(), time.process_time()
                for index in frame_order(self.meta["count"], mode, self.frames):
                    at = time.perf_counter()
                    _, timings, _ = self.frame(index, route, method)
                    rows.append({**timings, "total_ms": (time.perf_counter() - at) * 1000})
                elapsed = time.perf_counter() - wall
                result[f"{route}_{method}_{mode}"] = {
                    "fps": self.frames / elapsed,
                    "cpu_core_percent": (time.process_time() - cpu) / elapsed * 100,
                    "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                    "samples": rows,
                }
        parity = []
        for index in (0, min(30, self.clip.frame_count - 1), min(59, self.clip.frame_count - 1)):
            reference = self.reference(index)
            raw = self.clip.raw_frame(index).data
            for method in ("quad", "bilinear"):
                rgb, _ = self.gpu.render(raw, method, self.width, 0.0)
                parity.append({"index": index, "method": method, **difference(rgb, reference)})
        result.update(colour=parity, load_after=os.getloadavg())
        (self.output / "engine.json").write_text(json.dumps(result, indent=2) + "\n")

    def close(self) -> None:
        self.gpu.close()
        self.clip.close()


class Handler(BaseHTTPRequestHandler):
    trial: Trial

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def send(self, data: bytes, mime: str, timings: dict[str, float] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if timings:
            self.send_header("X-Timings", json.dumps(timings))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        trial = self.trial
        if parsed.path == "/":
            self.send((HERE / "page.html").read_bytes(), "text/html")
        elif parsed.path in ("/page.js", "/develop.frag"):
            self.send(
                (HERE / parsed.path[1:]).read_bytes(),
                "text/javascript" if parsed.path.endswith("js") else "text/plain",
            )
        elif parsed.path == "/phase":
            phase = query.get("name", ["unknown"])[0]
            (trial.output / "phase.json").write_text(json.dumps({"name": phase, "time": time.monotonic()}))
            self.send(b"ok", "text/plain")
        elif parsed.path == "/meta":
            self.send(json.dumps(trial.meta).encode(), "application/json")
        elif parsed.path == "/table":
            self.send(trial.table.astype("<f4").tobytes(), "application/octet-stream")
        elif parsed.path == "/calibration":
            self.send(trial.calibration.tobytes(), "application/octet-stream")
        elif parsed.path in ("/raw", "/compute", "/cpu"):
            index = int(query.get("index", ["0"])[0])
            data, timings, mime = trial.frame(index, parsed.path[1:], query.get("method", ["quad"])[0])
            self.send(data, mime, timings)
        elif parsed.path == "/reference":
            rgb = trial.reference(int(query.get("index", ["0"])[0]))
            self.send(rgb.tobytes(), "application/octet-stream")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/result":
            self.send_error(404)
            return
        result = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        label = result["browser"]
        if label not in ("chromium", "webkit"):
            self.send_error(400)
            return
        result["server"] = {
            "cpu_s": time.process_time() - self.trial.cpu_start,
            "wall_s": time.perf_counter() - self.trial.start,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        }
        (self.trial.output / f"{label}.json").write_text(json.dumps(result, indent=2) + "\n")
        self.send(b"ok", "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--port", type=int, default=18861)
    parser.add_argument("--engine-only", action="store_true")
    args = parser.parse_args()
    if args.frames < 1 or args.width < 16:
        parser.error("frames must be positive; width must be at least 16")
    trial = Trial(args.clip, args.output, args.frames, args.width)
    try:
        if args.engine_only:
            trial.engine_bench()
        else:
            Handler.trial = trial
            print(json.dumps(trial.meta), flush=True)
            trial.meta["ws_port"] = args.port + 1

            def frames(connection: ServerConnection) -> None:
                for message in connection:
                    request = json.loads(message)
                    index, route, method = request["index"], request["route"], request["method"]
                    if route not in ("raw", "cpu", "compute") or not 0 <= index < trial.clip.frame_count:
                        connection.close(1008, "Invalid frame request")
                        return
                    payload, timings, mime = trial.frame(index, route, method)
                    connection.send(json.dumps({"index": index, "mime": mime, "timings": timings}))
                    connection.send(payload)

            with (
                HTTPServer(("127.0.0.1", args.port), Handler) as server,
                serve(frames, "127.0.0.1", args.port + 1, compression=None, max_size=1 << 20) as websocket,
            ):
                worker = threading.Thread(target=websocket.serve_forever, name="trial-websocket")
                worker.start()
                try:
                    server.serve_forever()
                finally:
                    websocket.shutdown()
                    worker.join()
    finally:
        trial.close()


if __name__ == "__main__":
    main()
