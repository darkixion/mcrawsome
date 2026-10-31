"""Repeatable RAW engine baseline, using named recordings in place and disposable private state.

Example: uv run scripts/bench_raw_workflow.py CLIP --json /absolute/results.json
Each case runs in a fresh process; source/kernel/OpenCL caches may be warm. No system caches are
cleared. Timings include JPEG creation for previews, but exclude UI, network, display and sound.
Mount reads include SHA-256 in wall throughput, not in individual read latency. Timed loops reuse
normal service/kernel caches. Pacing includes WAV reads, but no audio device or editor. No media is saved.
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import math
import os
import platform
import random
import resource
import shutil
import statistics
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp
from typing import Any


def distribution(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max_ms": ordered[-1],
        "fps": 1000 / statistics.mean(ordered),
    }


def system_state() -> dict[str, Any]:
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    swap = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return {
        "time": time.time(),
        "monotonic": time.monotonic(),
        "boottime": time.clock_gettime(time.CLOCK_BOOTTIME),
        "ac_online": {
            path.parent.name: path.read_text().strip()
            for path in Path("/sys/class/power_supply").glob("*/online")
        },
        "thermal_c": {
            (path.parent / "type").read_text().strip(): int(path.read_text()) / 1000
            for path in Path("/sys/class/thermal").glob("thermal_zone*/temp")
        },
        "load": os.getloadavg(),
        "available_mib": int(memory["MemAvailable"].split()[0]) / 1024,
        "swap_in_pages": int(swap["pswpin"]),
        "swap_out_pages": int(swap["pswpout"]),
    }


class Memory:
    """Sample only the benchmark worker and its explicitly registered private service."""

    def __init__(self) -> None:
        self.pids = {os.getpid()}
        self.peak_mib = 0.0
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    def sample(self) -> None:
        while not self.done.is_set():
            total = 0
            for pid in tuple(self.pids):
                try:
                    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1])
                except OSError:
                    continue
            self.peak_mib = max(self.peak_mib, total / 1024)
            self.done.wait(0.05)

    def close(self) -> dict[str, float]:
        self.done.set()
        self.thread.join()
        return {
            "sampled_worker_and_service_rss_mib": self.peak_mib,
            "worker_high_water_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        }


def preview(config: dict[str, Any]) -> dict[str, Any]:
    from mcrawsome.preview.session import PreviewSession, PreviewSettings
    from mcrawsome.recipe.load import build_recipe

    recipe = build_recipe([{"develop": {"exposure": 0}, "motion": {"stabilise": "off"}}])
    settings = PreviewSettings(width=config["width"], cache_mib=0, gpu=config["backend"] == "opencl")
    result: dict[str, Any] = {}
    started = time.perf_counter()
    with PreviewSession(Path(config["clip"]), recipe, settings) as session:
        result["session_setup_ms"] = (time.perf_counter() - started) * 1000
        first = session.fast(0, width=config["width"])
        result["draft_size"] = first.size
        result["setup_to_first_jpeg_ms"] = (time.perf_counter() - started) * 1000
        result["backend"] = session.backend.name
        if config["backend"] == "opencl" and not session.backend.name.startswith("OpenCL:"):
            raise RuntimeError(session.backend.reason)
        count = min(config["frames"], session.frame_count - 1)
        timings, stages = [], []
        for index in range(1, count + 1):
            started = time.perf_counter()
            session.fast(index, width=config["width"], prefetch=True)
            timings.append((time.perf_counter() - started) * 1000)
            stages.append(dict(session.fast_timings))
        result["uncached_sequential"] = distribution(timings)
        result["sequential_stages"] = stages
        indices = random.Random(7).sample(
            range(session.frame_count), min(config["seeks"], session.frame_count)
        )
        timings = []
        for index in indices:
            started = time.perf_counter()
            session.fast(index, width=config["width"])
            timings.append((time.perf_counter() - started) * 1000)
        result["uncached_random"] = distribution(timings)
        started = time.perf_counter()
        source = session.source_encoder(session.frame_count // 2)()
        result["source_jpeg_ms"] = (time.perf_counter() - started) * 1000
        result["source_size"] = source.size
    with PreviewSession(
        Path(config["clip"]), recipe, settings.model_copy(update={"cache_mib": 256})
    ) as session:
        targets = indices[-4:]
        for index in targets:
            session.fast(index, width=config["width"])
        timings, hits = [], 0
        for index in targets * 4:
            started = time.perf_counter()
            session.fast(index, width=config["width"])
            timings.append((time.perf_counter() - started) * 1000)
            hits += int(session.fast_timings.get("cache_hit", 0))
        result["cached_steps"] = distribution(timings) | {"hits": hits}
        result["cache_bytes"] = session.cache_bytes
        assert session.cache_bytes <= 256 * 1024**2
    return result


def mounted(config: dict[str, Any], memory: Memory) -> dict[str, Any]:
    from mcrawsome.errors import McError
    from mcrawsome.mount.manager import MountedSequence, MountManager
    from mcrawsome.mount.sequence import source_stamp
    from mcrawsome.mount.service import ServingSequence
    from mcrawsome.recipe.raw import RawCalibration

    print("mount setup", file=sys.stderr, flush=True)
    source = Path(config["clip"])
    raw = RawCalibration(mount_proxy=config["proxy"])
    sequence = None
    manager = MountManager()
    started = time.perf_counter()
    if config["backend"] == "direct":
        folder = Path(os.environ["MCRAWSOME_STATE_DIR"]) / "mounts" / ("0" * 24)
        folder.mkdir(parents=True)
        record = MountedSequence(
            id="0" * 24, source=source, path=folder / "media", stamp=source_stamp(source), raw_settings=raw
        )
        sequence = ServingSequence(record, folder)
    else:
        record = manager.mount(source, raw_settings=raw)
        ready = Path(os.environ["MCRAWSOME_STATE_DIR"]) / "mounts" / record.id / "ready.json"
        memory.pids.add(json.loads(ready.read_bytes())["pid"])
    result: dict[str, Any] = {"mount_setup_ms": (time.perf_counter() - started) * 1000}
    try:
        names = (
            sequence.names() if sequence is not None else sorted(path.name for path in record.path.iterdir())
        )
        audio_names = [name for name in names if name.endswith(".wav")]
        frame_count = sum(name.endswith(".dng") for name in names)
        names = [name for name in names if name.endswith(".dng")][: config["frames"]]
        if not names:
            raise ValueError("no mounted DNG frames")
        duration = config.get("mount_duration", 0)
        pace = config.get("pace_fps", 0)
        audio_header = None
        audio_bytes = 0
        audio_hashes: dict[int, str] = {}
        frame_hashes: dict[str, str] = {}
        deadline_misses = 0
        lateness = []

        def read_range(name: str, offset: int, size: int) -> bytes:
            if sequence is not None:
                return bytes(sequence.read(name, offset, size))
            with (record.path / name).open("rb", buffering=0) as stream:
                stream.seek(offset)
                return stream.read(size)

        if pace and audio_names:
            audio_header = read_range(audio_names[0], 0, 44)
            if audio_header[:4] != b"RIFF" or audio_header[36:40] != b"data":
                raise ValueError("expected mounted PCM WAV header")
            channels = struct.unpack_from("<H", audio_header, 22)[0]
            sample_bytes = channels * 2
            sample_count = struct.unpack_from("<I", audio_header, 40)[0] // sample_bytes
        times, digests, total = [], [], 0
        wall = time.perf_counter()
        number = 0
        while number < len(names) or (duration and time.perf_counter() - wall < duration):
            position = number % len(names)
            name = names[position]
            if number % 120 == 0:
                print(f"reading frame {number}", file=sys.stderr, flush=True)
            if pace:
                time.sleep(max(0, wall + number / pace - time.perf_counter()))
            started = time.perf_counter()
            if sequence is not None:
                size = sequence.size(name)
                chunks = [sequence.read(name, at, 128 << 10) for at in range(0, size, 128 << 10)]
            else:
                with (record.path / name).open("rb", buffering=0) as stream:
                    chunks = list(iter(lambda: stream.read(128 << 10), b""))
            times.append((time.perf_counter() - started) * 1000)
            digest = hashlib.sha256()
            for chunk in chunks:
                digest.update(chunk)
                total += len(chunk)
            hashed = digest.hexdigest()
            if name in frame_hashes and frame_hashes[name] != hashed:
                raise ValueError(f"DNG changed on loop: {name}")
            frame_hashes[name] = hashed
            if number < len(names):
                digests.append(hashed)
            if audio_header is not None:
                # Use output-frame positions, avoiding cumulative sample-rounding drift.
                # Partial-source trials read only their corresponding audio range.
                # The WAV covers the complete mounted sequence, not the selected subset.
                first = round(position * sample_count / frame_count)
                stop = round((position + 1) * sample_count / frame_count)
                data = read_range(audio_names[0], 44 + first * sample_bytes, (stop - first) * sample_bytes)
                if len(data) != (stop - first) * sample_bytes:
                    raise ValueError("short mounted WAV read")
                hashed_audio = hashlib.sha256(data).hexdigest()
                if position in audio_hashes and audio_hashes[position] != hashed_audio:
                    raise ValueError("WAV changed on loop")
                audio_hashes[position] = hashed_audio
                audio_bytes += len(data)
            if pace:
                late = max(0, time.perf_counter() - (wall + (number + 1) / pace))
                lateness.append(late * 1000)
                deadline_misses += int(late > 0)
            number += 1
        elapsed = time.perf_counter() - wall
        late_summary = distribution(lateness) if lateness and any(lateness) else None
        if late_summary is not None:
            late_summary.pop("fps")  # A reciprocal lateness is not playback throughput.
        result |= {
            "reads": distribution(times),
            "wall_fps": len(times) / elapsed,
            "wall_mib_s": total / 1024**2 / elapsed,
            "bytes": total,
            "sha256": digests,
            "duration_s": elapsed,
            "unique_frames": len(names),
            "completed_loops": number // len(names),
            "frame_hashes": frame_hashes,
            "frame_read_ms": times if pace else [],
            "audio": {"bytes": audio_bytes, "sha256_by_frame": audio_hashes},
            "pacing": {
                "fps": pace,
                "deadline_misses": deadline_misses,
                "lateness_ms": lateness,
                "lateness": late_summary,
            },
        }
    finally:
        if sequence is not None:
            sequence.close()
        else:
            print("unmount", file=sys.stderr, flush=True)
            # Retry a transient busy result on this private mount only; never force-detach it.
            for attempt in range(4):
                try:
                    manager.unmount(record.id)
                    break
                except McError as exc:
                    if exc.code != "engine-busy" or attempt == 3:
                        raise
                    time.sleep(0.1 * (attempt + 1))
    return result


def worker(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("package"):
        sys.path.insert(0, config["package"])
    import cv2
    import numpy as np

    from mcrawsome.io.clip import Clip

    cv2.setNumThreads(1)
    before = system_state()
    faulthandler.dump_traceback_later(60, repeat=True)
    memory = Memory()
    try:
        result = preview(config) if config["mode"] == "preview" else mounted(config, memory)
        with Clip(Path(config["clip"]), metadata_only=True) as clip:
            meta = clip.frame_metadata(0)
            facts = {"frames": clip.frame_count, "fps": clip.fps, "size": [meta.width, meta.height]}
    finally:
        measured_memory = memory.close()
        faulthandler.cancel_dump_traceback_later()
    after = system_state()
    return config | {
        "suspend_seconds": (after["boottime"] - before["boottime"])
        - (after["monotonic"] - before["monotonic"]),
        "source_facts": facts,
        "result": result,
        "memory": measured_memory,
        "host_before": before,
        "host_after": after,
        "runtime": {"python": platform.python_version(), "numpy": np.__version__, "opencv": cv2.__version__},
    }


@contextmanager
def private_state(parent: Path) -> Iterator[Path]:
    """Never traverse a live FUSE filesystem during failure cleanup."""
    root = Path(mkdtemp(prefix="raw-bench-", dir=parent))
    try:
        yield root
    finally:
        media = root / "MCRAWSOME_MOUNT_DIR"
        for mount in media.iterdir() if media.is_dir() else []:
            if mount.is_mount():
                subprocess.run(["fusermount3", "-u", "--", str(mount)], check=True, timeout=15)
        shutil.rmtree(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path, nargs="?")
    parser.add_argument("--json", type=Path)
    parser.add_argument(
        "--package", type=Path, help="Import an extracted runtime from this dist-packages folder"
    )
    parser.add_argument("--mode", choices=["all", "preview", "mount"], default="all")
    parser.add_argument(
        "--mount-backends",
        nargs="+",
        choices=["direct", "fuse"],
        default=["direct", "fuse"],
        help="Limit mount trials when a backend (for example /dev/fuse) is unavailable",
    )
    parser.add_argument(
        "--mount-proxies",
        nargs="+",
        choices=["full", "half", "quarter", "eighth"],
        default=["full", "half", "quarter", "eighth"],
        help="Limit mount trials to selected proxy sizes",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--mount-duration", type=float, default=0, help="Loop selected frames for seconds")
    parser.add_argument(
        "--pace-fps", type=float, default=0, help="Pace reads and interleave mounted WAV ranges"
    )
    parser.add_argument("--seeks", type=int, default=24)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(not math.isfinite(value) or value < 0 for value in (args.mount_duration, args.pace_fps)):
        parser.error("duration and pace must be finite and nonnegative")
    if args.worker:
        print(json.dumps(worker(json.loads(args.worker))))
        return
    if args.clip is None or args.json is None or min(args.repeats, args.frames, args.seeks) < 1:
        parser.error("a clip, --json and positive repeat/frame/seek counts are required")
    clip = args.clip.resolve(strict=True)
    cases = []
    if args.mode != "mount":
        cases += [{"mode": "preview", "backend": backend} for backend in ("cpu", "opencl")]
    if args.mode != "preview":
        cases += [
            {"mode": "mount", "backend": backend, "proxy": proxy}
            for backend in args.mount_backends
            for proxy in args.mount_proxies
        ]
    report: dict[str, Any] = {
        "schema": 1,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "kernel": platform.release(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runs": [],
        "errors": [],
        "notes": (
            "Fresh processes and private state; warm OS/driver caches; "
            "one OpenCV/BLAS thread; normal service/kernel caches on loops; "
            "paced mounts interleave WAV reads; no audio-device/UI/Resolve timings."
        ),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeats):
        for case in cases if repeat % 2 == 0 else list(reversed(cases)):
            config = case | {
                "clip": str(clip),
                "package": str(args.package.resolve()) if args.package else None,
                "repeat": repeat,
                "frames": args.frames,
                "seeks": args.seeks,
                "width": args.width,
                "mount_duration": args.mount_duration,
                "pace_fps": args.pace_fps,
            }
            with private_state(args.json.parent) as temporary:
                environment = dict(
                    os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1"
                )
                for key in (
                    "MCRAWSOME_STATE_DIR",
                    "MCRAWSOME_CACHE_DIR",
                    "MCRAWSOME_MOUNT_DIR",
                    "XDG_CONFIG_HOME",
                    "XDG_CACHE_HOME",
                    "XDG_STATE_HOME",
                    "XDG_DATA_HOME",
                ):
                    environment[key] = str(Path(temporary) / key)
                try:
                    run = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(config)],
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=max(180, args.mount_duration + 120),
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    report["errors"].append(
                        config
                        | {
                            "timeout": max(180, args.mount_duration + 120),
                            "stderr": (exc.stderr or b"").decode(errors="replace"),
                        }
                    )
                    args.json.write_text(json.dumps(report, indent=2) + "\n")
                    print(f"TIMEOUT {case}", flush=True)
                    continue
                if run.returncode:
                    report["errors"].append(config | {"stderr": run.stderr, "stdout": run.stdout})
                    print(f"FAILED {case}: {run.stderr[-300:]}", flush=True)
                else:
                    report["runs"].append(json.loads(run.stdout))
                    print(f"Measured repeat {repeat + 1}: {case}", flush=True)
                args.json.write_text(json.dumps(report, indent=2) + "\n")
    groups: dict[str, list[dict[str, str]]] = {}
    for run in report["runs"]:
        if run["mode"] == "mount":
            groups.setdefault(run["proxy"], []).append(run["result"]["frame_hashes"])
    report["mount_checksums_match"] = all(
        all(value == values[0] for value in values) for values in groups.values()
    )
    args.json.write_text(json.dumps(report, indent=2) + "\n")
    if report["errors"] or not report["mount_checksums_match"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
