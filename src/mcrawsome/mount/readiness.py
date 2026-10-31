"""Check the actual virtual files before handing their paths to an editor."""

import os

from mcrawsome.mount.manager import MountedSequence


def readable_media(mount: MountedSequence) -> int:
    """Read the first and last TIFF headers without decoding pixels; return the first file's size.

    A live process and a FUSE mount entry do not prove its files are readable.
    Checks run only on an explicit send, never in Library scans or usage polling.
    """
    size = 0
    for index in dict.fromkeys((0, mount.frames - 1)):
        path = mount.path / f"{mount.source.stem}-{index:06d}.dng"
        try:
            with path.open("rb") as stream:
                header = stream.read(8)
                length = os.fstat(stream.fileno()).st_size
            if len(header) != 8 or header[:4] != b"II*\0" or length <= 8:
                raise ValueError("Invalid virtual TIFF header")
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Virtual media is not readable at {path}. Close its files in Resolve, then use Remount "
                f"in Virtual media and send again ({exc})"
            ) from exc
        if index == 0:
            size = length
    return size
