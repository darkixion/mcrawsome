"""Summarise retained browser results, process samples and timestamped intel_gpu_top output."""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
from pathlib import Path
from typing import Any


def analyse(folder: Path, browser: str) -> dict[str, Any]:
    result = json.loads((folder / f"{browser}.json").read_text())
    samples = json.loads((folder / f"{browser}-resources.json").read_text())["samples"]
    summaries = {}
    telemetry: dict[str, list[dict[str, Any]]] = {}
    timeline_path = folder / "gpu-top-timeline.json"
    if timeline_path.exists():
        timeline = json.loads(timeline_path.read_text())
        phases = timeline["phases"]
        stamps = [row["time"] for row in phases]
        elapsed = timeline["started"]
        gpu = json.loads((folder / "gpu-top.json").read_text())
        for row in gpu:
            elapsed += row["period"]["duration"] / 1000
            index = bisect.bisect_right(stamps, elapsed) - 1
            if index >= 0:
                telemetry.setdefault(phases[index]["phase"], []).append(row)
    for key, run in result["runs"].items():
        rows = [row for row in samples if row["phase"] == key]
        if len(rows) < 2:
            raise ValueError(f"Insufficient process samples for {key}")
        first, last = rows[0], rows[-1]
        seconds = last["time"] - first["time"]
        summary = {
            "fps": run["fps"],
            "median_ms": {field: value["median"] for field, value in run.items() if isinstance(value, dict)},
            "server_cpu_percent": (last["server"]["cpu_s"] - first["server"]["cpu_s"]) / seconds * 100,
            "browser_cpu_percent": (last["browser"]["cpu_s"] - first["browser"]["cpu_s"]) / seconds * 100,
            "server_rss_mib": max(row["server"]["rss_mib"] for row in rows),
            "browser_rss_mib": max(row["browser"]["rss_mib"] for row in rows),
            "client_gpu_busy_percent": {},
        }
        for source in ("server", "browser"):
            a, b = first[source]["engines_ns"], last[source]["engines_ns"]
            summary["client_gpu_busy_percent"][source] = {
                engine: (value - a.get(engine, 0)) / (seconds * 1e9) * 100 for engine, value in b.items()
            }
        gpu_rows = telemetry.get(key, [])
        if gpu_rows:
            total = sum(row["period"]["duration"] for row in gpu_rows)
            summary["whole_gpu_busy_percent"] = {
                engine: sum(row["engines"][engine]["busy"] * row["period"]["duration"] for row in gpu_rows)
                / total
                for engine in gpu_rows[0]["engines"]
            }
            summary["gpu_samples"] = len(gpu_rows)
        summaries[key] = summary
    return {
        "browser": browser,
        "renderer": result["renderer"],
        "timer_extension": result["timerExtension"],
        "runs": summaries,
        "colour": result["colour"],
        "median_colour_mean": statistics.median(row["mean_8bit"] for row in result["colour"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--browser", choices=("chromium", "webkit"), required=True)
    args = parser.parse_args()
    print(json.dumps(analyse(args.folder, args.browser), indent=2))


if __name__ == "__main__":
    main()
