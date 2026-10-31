"""The colour space MotionCam records in a file name, for the "Auto" choice.

MotionCam names a clip `<date>_<time>_VIDEO_<focal length>_<transfer>_<gamut>.<extension>`, for example
`250930_133630_VIDEO_25mm_Davinci_Davinci-WG.gcsv`. Only tokens seen on the owner's phone are listed;
RAW names have no colour tokens and return None.
"""

from __future__ import annotations

from pathlib import Path

# MotionCam's gamut token -> MCRAWsome gamut id (`mcrawsome.colour.spaces.GAMUTS`).
MOTIONCAM_GAMUT_TOKENS: dict[str, str] = {
    "Davinci-WG": "davinci-wg",
    "BT709": "rec709",
    "sRGB": "rec709",
    "Rec2020": "rec2020",
    "Panasonic-WG": "v-gamut",
    "ARRI-WG3": "awg3",
    "ACES-AP1": "ap1",
    "Canon": "cinema-gamut",
    "S-Gamut3C": "s-gamut3-cine",
    "Native": "camera-native",
    "Apple-Log2": "apple-wide-gamut",
    "Display-P3": "display-p3",
}

# MotionCam's transfer token -> MCRAWsome transfer id (`mcrawsome.colour.transfer.TRANSFERS`).
MOTIONCAM_TRANSFER_TOKENS: dict[str, str] = {
    "Davinci": "davinci-intermediate",
    "BT.709": "bt709",
    "HLG": "hlg",
    "CINEON": "cineon",
    "PQ": "pq",
    "sRGB": "srgb",
    "Panasonic VLOG": "v-log",
    "ACES CCT": "acescct",
    "Canon Log 3": "canon-log3",
    "Sony S-Log3": "s-log3",
    "Apple Log": "apple-log",
    "LogC3": "logc3",
    "Samsung Log": "samsung-log",
    "F-Log": "f-log",
    "Linear": "linear",
}


def colour_from_name(name: str | Path) -> tuple[str, str] | None:
    """The (gamut id, transfer id) a MotionCam file name carries, or None without recognised tokens."""
    stem = _stem(Path(name).name)
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    gamut = MOTIONCAM_GAMUT_TOKENS.get(parts[-1])
    transfer = MOTIONCAM_TRANSFER_TOKENS.get(parts[-2])
    if gamut is None or transfer is None:
        return None
    return gamut, transfer


def _stem(name: str) -> str:
    """The name without its extension; a dot inside a token (as in `BT.709_BT709`) is not one."""
    base, dot, ext = name.rpartition(".")
    return base if dot and ext and "_" not in ext else name
