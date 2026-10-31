"""mcrawsome: open-source tools for MotionCam Pro RAW video."""

import sys
from pathlib import Path

__version__ = "0.1.0"

# The .deb writes VENDOR, naming its private folder of the Python packages Debian lacks or has too old.
# They come before Debian's own, for MCRAWsome's processes only; a development install has no VENDOR.
_vendor = Path(__file__).with_name("VENDOR")
if _vendor.is_file():
    _folder = _vendor.read_text().strip()
    if _folder and _folder not in sys.path:
        sys.path.insert(0, _folder)
