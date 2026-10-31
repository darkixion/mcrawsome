"""Measure sequential CinemaDNG reads through a private, temporary FUSE mount.

Run with a named local clip: python scripts/bench_mount.py CLIP --start 100 --frames 96 --json PATH
Use --direct to measure the production sequence service without FUSE, and --baseline with --direct
to disable its one-frame read-ahead for an A/B comparison. Source filesystem caches may be warm.
This measures RAW byte delivery, not Resolve playback.
Every repeat remounts to avoid reusing the virtual DNG kernel cache. No source data is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mcrawsome.mount.manager import MountedSequence, MountManager
from mcrawsome.mount.sequence import source_stamp
from mcrawsome.mount.service import ServingSequence


class DemandOnly(ServingSequence):
    """Benchmark control: identical service, with no speculative work."""

    def _decode(self, index):  # type: ignore[no-untyped-def]
        return self._decode_demand(index)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--frames", type=int, default=96)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--direct", action="store_true", help="Exercise the service without a FUSE mount")
    parser.add_argument("--baseline", action="store_true", help="Disable read-ahead; requires --direct")
    args = parser.parse_args()
    if min(args.start, args.frames - 1, args.repeats - 1) < 0:
        parser.error("start must be nonnegative; frames and repeats must be positive")
    if args.baseline and not args.direct:
        parser.error("--baseline requires --direct")
    runs: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="mcrawsome-mount-bench-") as temporary:
        root = Path(temporary)
        for key, name in (
            ("MCRAWSOME_STATE_DIR", "state"),
            ("MCRAWSOME_CACHE_DIR", "cache"),
            ("MCRAWSOME_MOUNT_DIR", "media"),
            ("XDG_CONFIG_HOME", "config"),
        ):
            os.environ[key] = str(root / name)
        manager = MountManager()
        for repeat in range(args.repeats):
            sequence = None
            if args.direct:
                source = args.clip.resolve()
                folder = root / "state" / "mounts" / str(repeat)
                folder.mkdir(parents=True)
                record = MountedSequence(
                    id=f"{repeat:024x}", source=source, path=root / "media", stamp=source_stamp(source)
                )
                sequence = (DemandOnly if args.baseline else ServingSequence)(record, folder)
            else:
                record = manager.mount(args.clip.resolve())
            try:
                files = (
                    [record.path / name for name in sequence.names() if name.endswith(".dng")]
                    if sequence is not None
                    else sorted(record.path.glob("*.dng"))
                )[args.start : args.start + args.frames]
                if len(files) != args.frames:
                    raise ValueError("requested range exceeds the mounted sequence")
                # Include stat and open costs; hash outside the measured delivery interval.
                times, checksums, total = [], [], 0
                wall_started = time.perf_counter()
                for path in files:
                    started = time.perf_counter()
                    if sequence is not None:
                        size = sequence.size(path.name)
                        chunks = [sequence.read(path.name, at, 128 << 10) for at in range(0, size, 128 << 10)]
                    else:
                        with path.open("rb", buffering=0) as stream:
                            chunks = []
                            while chunk := stream.read(128 << 10):
                                chunks.append(chunk)
                    times.append(time.perf_counter() - started)
                    digest = hashlib.sha256()
                    for chunk in chunks:
                        digest.update(chunk)
                        total += len(chunk)
                    checksums.append(digest.hexdigest())
                runs.append(
                    {
                        "seconds": sum(times),
                        "fps": len(files) / sum(times),
                        # Read-ahead can overlap client processing. Include hashing in wall throughput;
                        # read-only timings describe consumer stalls, not the decoder's maximum FPS.
                        "wall_seconds": (wall_seconds := time.perf_counter() - wall_started),
                        "wall_fps": len(files) / wall_seconds,
                        "median_frame_ms": statistics.median(times) * 1000,
                        "p95_frame_ms": sorted(times)[int(len(times) * 0.95)] * 1000,
                        "bytes": total,
                        "sha256": checksums,
                    }
                )
            finally:
                if sequence is not None:
                    sequence.close()
                else:
                    manager.unmount(record.id)
    assert all(run["sha256"] == runs[0]["sha256"] for run in runs)
    report = {
        "source": str(args.clip.resolve()),
        "start": args.start,
        "frames": args.frames,
        "backend": "service without FUSE" if args.direct else "FUSE",
        "read_ahead": not args.baseline,
        "note": (
            "Sequential RAW bytes; source caches may be warm; hashing excluded from read timings "
            "but included in wall throughput; not Resolve FPS"
        ),
        "median_fps": statistics.median(run["fps"] for run in runs),
        "runs": runs,
        "median_wall_fps": statistics.median(run["wall_fps"] for run in runs),
    }
    output = json.dumps(report, indent=2) + "\n"
    if args.json:
        args.json.write_text(output)
    print(output)


if __name__ == "__main__":
    main()
