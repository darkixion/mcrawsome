"""Compare mounted PCM against the separate export alignment path using recordings in place.

Checks recorded, 25 fps and nearest NTSC rates, both complete and trimmed sequences. No audio
device, media export, owner configuration or native editor is involved. Private recordings must
remain outside version control; the JSON contains measurements and PCM hashes only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def check(source: Path) -> list[dict[str, Any]]:
    from mcrawsome.io.clip import Clip
    from mcrawsome.mount.sequence import Sequence
    from mcrawsome.output.audio import aligned_track
    from mcrawsome.recipe.raw import RawCalibration

    results = []
    for rate in ("keep", 25.0, "drop-frame"):
        settings = RawCalibration.model_validate({"mount_frame_rate": rate})
        for trim in (None, (3, 17)):
            with Sequence(source, raw_settings=settings, frame_range=trim) as sequence, Clip(source) as clip:
                track = clip.audio()
                if track is None or sequence.wave is None:
                    raise ValueError(f"no recorded audio: {source}")
                count = len(sequence.sources)
                sample_rate = track.sample_rate
                begin = round(sequence.first / float(sequence.rate) * sample_rate)
                length = round(count / float(sequence.rate) * sample_rate)
                # Align independently, then slice output samples. WaveView instead resolves
                # source segments/ranges on demand, including the trim offset and tempo.
                expected = aligned_track(
                    track, int(clip.timestamps_ns[0]), (begin + length) / sample_rate, sequence.tempo
                )
                expected_pcm = expected.samples[begin : begin + length].astype("<i2").tobytes()
                name = source.stem + ".wav"
                size = sequence.size(name)
                data = b"".join(
                    bytes(sequence.read(name, at, min(131071, size - at))) for at in range(0, size, 131071)
                )
                if data[44:] != expected_pcm:
                    raise ValueError(f"PCM mismatch: {source.name}, rate={rate}, trim={trim}")
                if len(expected_pcm) != length * track.samples.shape[1] * 2:
                    raise ValueError("unexpected aligned PCM length")
                results.append(
                    {
                        "source": source.name,
                        "rate": rate,
                        "trim": trim,
                        "output_frames": count,
                        "sample_frames": length,
                        "tempo": sequence.tempo,
                        "pcm_sha256": hashlib.sha256(expected_pcm).hexdigest(),
                    }
                )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clips", type=Path, nargs="+")
    parser.add_argument("--package", type=Path, help="Extracted runtime dist-packages directory")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    if args.package:
        sys.path.insert(0, str(args.package.resolve(strict=True)))
    results = [result for clip in args.clips for result in check(clip.resolve(strict=True))]
    args.json.write_text(json.dumps(results, indent=2) + "\n")
    print(f"{len(results)} real-source audio comparisons passed")


if __name__ == "__main__":
    main()
