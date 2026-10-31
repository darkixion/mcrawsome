"""Find how DaVinci Resolve uses Camera Gyro's twelve camera-row pose slots.

Writes one native timeline with three copies of a mounted RAW clip. Each copy has no correction except
a sharp pan in a single row slot (the first, the middle and the last). In Resolve, the part of the picture
that moves shows which rows a slot controls, and the edge of the moved part (a hard step or a gradient)
shows how Resolve blends between slots. That fixes the convention MCRAWsome's rolling-shutter rows need.

    uv run python scripts/resolve_row_probe.py ~/Downloads/motioncam/261004_174122_VIDEO_26mm.mcraw \
        ~/Downloads/row-probe.drt

Only the owner-approved recording 261004_174122 is accepted. The clip is mounted (full resolution, recorded
orientation) with its own state folder (~/.cache/mcrawsome-row-probe) and mount folder
(~/Mounts/mcrawsome-row-probe), apart from the app's, and stays mounted until `--unmount`. Nothing is sent
or opened automatically.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from mcrawsome.mount.manager import MountManager
from mcrawsome.resolve.bin_archive import mounted_media
from mcrawsome.resolve.camera_gyro import ROWS, CameraGyroCorrections
from mcrawsome.resolve.timeline_archive import _write

APPROVED = "261004_174122"
STATE = Path.home() / ".cache/mcrawsome-row-probe/state"
MOUNTS = Path.home() / "Mounts/mcrawsome-row-probe"
SLOTS = (0, ROWS // 2, ROWS - 1)  # the first, a middle and the last camera-row slot
PAN_DEG = 3.0


def probe(
    media_frames: int, rate: float, width: int, height: int, orientation: int, slot: int
) -> CameraGyroCorrections:
    poses = np.tile(np.array([0, 0, 0, 1], np.float32), (media_frames, ROWS, 1))
    poses[:, slot] = Rotation.from_euler("y", PAN_DEG, degrees=True).as_quat().astype(np.float32)
    times = (np.arange(media_frames) / rate + 0.01) * 1e9
    corrections = CameraGyroCorrections(
        poses, times.astype(np.float32), width * 0.8, 1.0, width, height, orientation
    )
    corrections.validate(media_frames, width, height, orientation)
    return corrections


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("clip", type=Path, nargs="?", help=f"the {APPROVED} .mcraw recording")
    parser.add_argument("output", type=Path, nargs="?", help="the .drt to write (must not exist)")
    parser.add_argument("--unmount", action="store_true", help="unmount the probe's media and stop")
    args = parser.parse_args()
    os.environ["MCRAWSOME_STATE_DIR"] = str(STATE)  # the probe's mounts are not the app's
    if args.unmount:
        manager = MountManager()
        for mounted in manager.listing().mounts:
            if mounted.active:
                manager.unmount(mounted.id)
                print(f"Unmounted {mounted.path}")
        return 0
    if args.clip is None or args.output is None:
        parser.error("give the clip and the output .drt (or --unmount)")
    clip: Path = args.clip.expanduser().resolve()
    if APPROVED not in clip.name:
        parser.error(f"only the approved recording {APPROVED} may be used")
    output: Path = args.output.expanduser()
    if output.exists():
        parser.error(f"{output} exists")
    mount = MountManager().mount(clip, location=MOUNTS)
    media = mounted_media(mount)
    info = media.info
    corrections = [
        probe(media.frames, media.rate, info.width, info.height, info.orientation, slot) for slot in SLOTS
    ]
    _write(output, [media] * len(SLOTS), name="mcrawsome row-slot probe", corrections=corrections)
    print(f"Wrote {output}, three clips of {clip.name} mounted at {mount.path}.")
    print("Import it in DaVinci Resolve (File > Import > Timeline), then for each clip in order:")
    for number, slot in enumerate(SLOTS, 1):
        print(f"  clip {number}: only row slot {slot + 1} of {ROWS} is panned {PAN_DEG} degrees")
    print("Stop on a frame in the middle of each clip and save a still or screenshot of the viewer,")
    print("plus one of the same frame with the Camera Gyro correction switched off, for comparison.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
