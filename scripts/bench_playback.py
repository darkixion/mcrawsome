"""Measure native playback and slider drafts on an explicitly named local clip.

uv run scripts/bench_playback.py CLIP --json PATH
No frames or clip data are saved. The normal interface and AppImage should also be checked for
clock timing, JPEG display and sound, which these engine timings do not measure.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import time
from pathlib import Path
from typing import Any

from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import build_recipe


def stats(times: list[float]) -> dict[str, float]:
    ordered = sorted(times)
    return {
        "median_ms": statistics.median(times),
        "p95_ms": ordered[int(len(times) * 0.95)],
        "max_ms": max(times),
        "fps": 1000 / statistics.mean(times),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--backend", choices=["auto", "cpu", "opencl"], default="auto")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result: dict[str, Any] = {"clip": args.clip.name, "width": args.width, "load_before": os.getloadavg()}
    recipe = build_recipe([{"develop": {"exposure": 0}}])
    started = time.perf_counter()
    settings = PreviewSettings(gpu=args.backend != "cpu")
    with PreviewSession(args.clip, recipe, settings) as session:
        session.set_recipe(recipe, fast=True)
        frame = session.fast(0, width=args.width, prefetch=True)
        result["first_ms"] = (time.perf_counter() - started) * 1000
        result["size"] = frame.size
        timings = []
        stages = []
        wall, cpu = time.perf_counter(), time.process_time()
        for index in range(1, min(args.frames + 1, session.frame_count)):
            started = time.perf_counter()
            session.fast(index, width=args.width, prefetch=True)
            timings.append((time.perf_counter() - started) * 1000)
            stages.append(dict(session.fast_timings))
        result["cpu_core_percent"] = (time.process_time() - cpu) / (time.perf_counter() - wall) * 100
        result["backend"] = session.backend.name
        result["backend_reason"] = session.backend.reason
        result["stages"] = stages
        if args.backend == "opencl" and not session.backend.name.startswith("OpenCL:"):
            raise RuntimeError(session.backend.reason)
        result["frames"] = len(timings)
        result["playback"] = stats(timings)
        for field, section, values in (
            ("exposure", "develop", [i / 10 for i in range(-10, 11)]),
            ("warmth", "grade", [i / 100 for i in range(-10, 11)]),
            ("sat", "grade", [i / 10 for i in range(5, 26)]),
            ("crop", "motion", [1 + i / 100 for i in range(21)]),
        ):
            timings = []
            for value in values:
                changed = build_recipe([{"develop": {"exposure": 0}}, {section: {field: value}}])
                started = time.perf_counter()
                session.set_recipe(changed, fast=True)
                session.fast(index, width=args.width)
                timings.append((time.perf_counter() - started) * 1000)
            result[field] = stats(timings)
        result["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        result["load_after"] = os.getloadavg()
    text = json.dumps(result, indent=2)
    print(text)
    if args.json:
        args.json.write_text(text + "\n")


if __name__ == "__main__":
    main()
