"""Generate reference outputs from the prototype (one-off, run by a developer).

Usage: uv run python scripts/make_references.py /home/thom/Downloads/motioncam/grading
Writes tests/fixtures/data/ref_develop_12bit.npz (gitignored) and tests/data/ref_motion.npz (committed).
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np

from mcrawsome.io.clip import Clip

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    prototype = Path(sys.argv[1])
    sys.path.insert(0, str(prototype))
    import develop  # type: ignore[import-not-found]  # the prototype's develop.py

    clip_path = ROOT / "tests" / "fixtures" / "data" / "clip_12bit.mcraw"
    with Clip(clip_path) as clip, clip_path.open("rb") as f:
        frame = clip.raw_frame(0)
        from mcrawsome.io.container import read_frame_record, read_index

        idx = read_index(f)
        frame0 = json.loads(read_frame_record(f, idx.frames[0].offset)[1])
        info = {"container": idx.metadata, "frame0": frame0}
        out = develop.Developer(info).develop(frame.data.ravel())
    small = cv2.resize(out, (960, 540), interpolation=cv2.INTER_AREA)
    np.savez_compressed(ROOT / "tests" / "fixtures" / "data" / "ref_develop_12bit.npz", image=small)

    np.savez_compressed(ROOT / "tests" / "data" / "ref_motion.npz", **motion_references())
    print("references written")


def synthetic_gyro() -> tuple[np.ndarray, np.ndarray]:
    """A 4-second, 300 Hz gyro log in rad/s: slow pan, hand shake and noise (device axes)."""
    rng = np.random.default_rng(9)
    t = np.arange(0, 4, 1 / 300)
    gyro = np.stack(
        [
            0.15 + 0.4 * np.sin(2 * np.pi * 3.1 * t) + 0.02 * rng.standard_normal(len(t)),
            0.3 * np.sin(2 * np.pi * 4.3 * t + 1.0) + 0.02 * rng.standard_normal(len(t)),
            0.1 * np.sin(2 * np.pi * 2.2 * t + 0.4) + 0.01 * rng.standard_normal(len(t)),
        ],
        -1,
    )
    return t, gyro


def motion_references() -> dict[str, np.ndarray]:
    import stab  # type: ignore[import-not-found]  # the prototype's stab.py

    t, gyro = synthetic_gyro()
    orient = stab.Orientation(t, gyro, axes=[0, 1, 2], signs=[1, -1, 1])
    frames = np.arange(0.1, 3.9, 1 / 24)
    q = orient.at(frames)
    smoothed = stab.smooth_quats(q, 12.0)
    rotations = stab.quat_to_mat(stab.quat_mul(smoothed, q * np.array([1, -1, -1, -1])))
    bands = np.stack([rotations[10], rotations[40], rotations[80]])[:, None].repeat(6, 1)  # (3, 6, 3, 3)
    bands[:, :, 0, 2] += np.linspace(-0.002, 0.002, 6)  # make the bands differ, like rolling shutter
    focal, zoom, width, height = 190.0, 1.1, 240, 135
    k = np.array([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]])
    maps = [stab.warp_maps(b, k, zoom, width, height) for b in bands]
    return {
        "times": t,
        "gyro": gyro,
        "frame_times": frames,
        "q": q,
        "q_smooth": smoothed,
        "bands": bands,
        "map_x": np.stack([m[0] for m in maps]),
        "map_y": np.stack([m[1] for m in maps]),
    }


if __name__ == "__main__":
    main()
