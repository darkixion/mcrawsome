"""Compare metadata reads (no image decoding). Pass local .mcraw paths; emits JSON.

The legacy path re-reads buffer headers via the buffered file. The new path
uses validated metadata offsets and unbuffered reads, including index startup.
This measures application reads, not physical disk/cache misses. ABBA order
reduces warm-cache bias; no system caches or user files are changed.
"""

import hashlib
import io
import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

from mcrawsome.io.clip import Clip
from mcrawsome.io.container import read_frame_metadata


def trial(path: Path, direct: bool) -> dict:
    read_bytes = read_calls = 0
    original = Path.open

    class Reader(io.FileIO):
        def read(self, count: int = -1) -> bytes:
            nonlocal read_bytes, read_calls
            result = super().read(count)
            read_bytes += len(result)
            read_calls += 1
            return result

        def readinto(self, buffer) -> int:
            nonlocal read_bytes, read_calls
            count = super().readinto(buffer)
            read_bytes += count
            read_calls += 1
            return count

    def opened(self: Path, *args, **kwargs):
        if self != path:
            return original(self, *args, **kwargs)
        raw = Reader(str(path), "rb")
        return raw if kwargs.get("buffering") == 0 else io.BufferedReader(raw)

    started = time.perf_counter()
    digest = hashlib.sha256()
    with patch.object(Path, "open", opened), Clip(path, metadata_only=direct) as clip:
        for frame in clip._index.frames:
            data = read_frame_metadata(clip._f, frame.offset, ref=frame if direct else None)
            json.loads(data)  # include the identical JSON parsing work on both paths
            digest.update(data)
        clip.check_unchanged()
        frames = clip.frame_count
    return {
        "direct": direct,
        "seconds": time.perf_counter() - started,
        "frames": frames,
        "application_read_bytes": read_bytes,
        "read_calls": read_calls,
        "metadata_sha256": digest.hexdigest(),
    }


def main() -> None:
    results = []
    for name in sys.argv[1:]:
        path = Path(name).resolve(strict=True)
        runs = [trial(path, direct) for direct in (False, True, True, False)]
        assert len({run["metadata_sha256"] for run in runs}) == 1
        results.append(
            {
                "source": str(path),
                "source_bytes": path.stat().st_size,
                "runs": runs,
                "median_seconds": {
                    str(direct): statistics.median(run["seconds"] for run in runs if run["direct"] == direct)
                    for direct in (False, True)
                },
            }
        )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
