"""Shared timeline geometry checks, allowing only even-pixel fitting round-off."""


def matching_aspect(source: tuple[int, int], target: tuple[int, int]) -> bool:
    """At most one output pixel of rounding on the fitted edge; no substantive aspect change."""
    sw, sh = source
    tw, th = target
    return abs(sw * th - sh * tw) <= max(sw, sh)
