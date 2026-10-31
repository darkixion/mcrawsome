"""A file manager's thumbnail of a RAW recording: its first frame as playback drafts it, developed
neutrally (camera white balance, no exposure change, the neutral display rendering) and at most SIZE
pixels on its longer side, as a PNG.

GNOME Files runs a thumbnailer in a sandbox that holds the recording alone, read-only, with only /usr and
no network, under an environment whose home folder is not there. So nothing here reads the settings
beside the recording or in the user's folders, uses the GPU, writes anything but the thumbnail, or starts
another process; the recording shows as recorded, without its saved display turn.

Usage (packaging/linux/mcrawsome.thumbnailer): python3 -I -m mcrawsome.thumbnailer SIZE INPUT OUTPUT
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.schema import DevelopParams, MotionParams, Recipe

LARGEST = 1024  # GNOME's xx-large thumbnails; larger requests are made at this size


def thumbnail(clip: Path, size: int) -> npt.NDArray[np.uint8]:
    """The first frame as 8-bit BGR, at most `size` pixels on its longer side."""
    size = max(16, min(size, LARGEST))
    recipe = Recipe(develop=DevelopParams(exposure=0), motion=MotionParams(stabilise="off"))
    # The defaults, given rather than read: the user's settings are outside the sandbox.
    settings = PreviewSettings(width=size, gpu=False, quad_bayer_remosaic=True, cache_mib=0, fast_open=True)
    with PreviewSession(clip, recipe, settings) as session:
        width, height = session.full_size
        bgr = session.still(0, size if width >= height else max(1, round(size * width / height)))
    if max(bgr.shape[:2]) > size:  # a portrait frame drafted a pixel too tall
        scale = size / max(bgr.shape[:2])
        shape = (max(1, round(bgr.shape[1] * scale)), max(1, round(bgr.shape[0] * scale)))
        bgr = np.asarray(cv2.resize(bgr, shape, interpolation=cv2.INTER_AREA))
    return bgr


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 3 or not arguments[0].isdecimal():
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    size, clip, output = int(arguments[0]), Path(arguments[1]), Path(arguments[2])
    try:
        ok, png = cv2.imencode(".png", thumbnail(clip, size))
        if not ok:
            raise ValueError("the thumbnail could not be encoded as PNG")
        output.write_bytes(png.tobytes())
    except Exception as exc:  # the file manager shows its generic icon; say why for its log
        print(f"{clip.name}: no thumbnail ({exc})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
