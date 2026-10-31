"""Measure random seeks and warmed steps on one explicitly named local clip, without saving images.

Use the native window too: these timings include JPEG decoding but exclude network and presentation.
Run intel_gpu_top -J alongside this command; each sample records its wall-clock time.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from bench_playback import stats

from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import build_recipe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--backend", choices=["cpu", "opencl"], required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    settings = PreviewSettings(gpu=args.backend == "opencl")
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    result: dict[str, Any] = {"clip": args.clip.name, "load_before": os.getloadavg()}
    with PreviewSession(args.clip, recipe, settings) as session:
        session.set_recipe(recipe, fast=True)
        opened = time.perf_counter()
        session.fast(0)
        result["first_draft_ms"] = (time.perf_counter() - opened) * 1000
        centre = session.frame_count // 2
        phases = {
            "random_exact": (1280, random.Random(7).sample(range(session.frame_count), 40)),
            "drag": (640, [round(i * (session.frame_count - 1) / 59) for i in range(60)]),
            "warm_step": (1280, [centre + d for _ in range(8) for d in (-1, 1, -2, 2)]),
        }
        for name, (width, indices) in phases.items():
            if name == "warm_step":
                for index in range(centre - 4, centre + 5):
                    session.fast(index, width=1280)
            rows = []
            wall, cpu = time.perf_counter(), time.process_time()
            for index in indices:
                started = time.perf_counter()
                frame = session.fast(index, width=width)
                encoded = time.perf_counter()
                image = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
                assert image is not None
                rows.append(
                    dict(session.fast_timings)
                    | {
                        "index": index,
                        "at": time.time(),
                        "image_decode_ms": (time.perf_counter() - encoded) * 1000,
                        "input_to_decoded_ms": (time.perf_counter() - started) * 1000,
                    }
                )
            result[name] = {
                "latency": stats([row["input_to_decoded_ms"] for row in rows]),
                "cpu_core_percent": (time.process_time() - cpu) / (time.perf_counter() - wall) * 100,
                "cache_bytes": getattr(session, "cache_bytes", 0),
                "samples": rows,
            }
        result["backend"] = session.backend.name
        if args.backend == "opencl" and not session.backend.name.startswith("OpenCL:"):
            raise RuntimeError(session.backend.reason)
        result["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        result["load_after"] = os.getloadavg()
    args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(args.json)


if __name__ == "__main__":
    main()
