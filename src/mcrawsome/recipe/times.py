"""Times in recipes and on the command line: a frame index from the forms people write."""

from __future__ import annotations

import re

_FORMS = (
    "seconds (5 or 5s or 0.5s), minutes and seconds (1:05 or 1:05.5), "
    "hours, minutes and seconds (01:02:03.5), or frames (120f)"
)


def parse_time(text: str, fps: float) -> int:
    """A frame index from seconds, minutes and seconds, hours, minutes and seconds, or frames."""
    try:
        return _parse_time(text, fps)
    except OverflowError:
        raise ValueError(f"the time {text!r} is too large; use {_FORMS}") from None


def _parse_time(text: str, fps: float) -> int:
    value = text.strip()
    if match := re.fullmatch(r"(\d+)f", value):
        return int(match.group(1))
    if match := re.fullmatch(r"(\d+(?:\.\d+)?)s?", value):
        return round(float(match.group(1)) * fps)
    if (match := re.fullmatch(r"(\d+):(\d{2}(?:\.\d+)?)", value)) and float(match.group(2)) < 60:
        return round((int(match.group(1)) * 60 + float(match.group(2))) * fps)
    if (
        (match := re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", value))
        and int(match.group(2)) < 60
        and float(match.group(3)) < 60
    ):
        return round((int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))) * fps)
    raise ValueError(f"cannot read the time {text!r}; use {_FORMS}")
