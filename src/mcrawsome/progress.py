"""Progress reporting shared by exports and previews."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

Phase = Literal["measuring", "stabilising", "hdr-prepass", "rendering", "muxing", "verifying"]

# (phase, done, total); total is 0 while it is not known
Progress = Callable[[Phase, int, int], None]
