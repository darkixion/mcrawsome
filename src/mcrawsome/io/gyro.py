"""Gyro logs from any source (.gcsv files or telemetry inside a video)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

MAX_GAP_S = 0.25


@dataclass(frozen=True)
class GyroLog:
    """Angular rate samples in rad/s on the device's own axes.

    `clock` says how the times relate to the frames: "epoch" logs (.gcsv) run on the phone's wall clock
    from `start_epoch_s` and link to frames through their receive times; "video" logs (embedded
    telemetry) share the video's own timeline.
    """

    times_s: npt.NDArray[np.float64]
    gyro: npt.NDArray[np.float64]
    accel: npt.NDArray[np.float64] | None
    start_epoch_s: float | None
    orientation: str
    header: dict[str, str] = field(default_factory=dict)
    clock: Literal["epoch", "video"] = "epoch"

    def holes(self) -> npt.NDArray[np.float64]:
        """(start, end) of every gap between samples longer than MAX_GAP_S. Shorter gaps (a telemetry
        block with no samples is about one frame) are bridged by interpolation."""
        t = self.times_s
        if len(t) < 2:
            return np.zeros((0, 2))
        wide = np.flatnonzero(np.diff(t) > MAX_GAP_S)
        return np.asarray(np.stack([t[wide], t[wide + 1]], -1), dtype=np.float64).reshape(-1, 2)

    def coverage(self, t_start: float, t_end: float) -> float:
        """The fraction of [t_start, t_end] that the log covers, holes excluded."""
        if t_end <= t_start or len(self.times_s) == 0:
            return 0.0
        lo, hi = max(t_start, float(self.times_s[0])), min(t_end, float(self.times_s[-1]))
        if hi <= lo:
            return 0.0
        holes = self.holes()
        missing = float(np.clip(np.minimum(holes[:, 1], hi) - np.maximum(holes[:, 0], lo), 0, None).sum())
        return max(0.0, hi - lo - missing) / (t_end - t_start)
