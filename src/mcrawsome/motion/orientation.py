"""Camera orientation over time, integrated from a gyro log."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.geometry import quat_from_rotvec

Array = npt.NDArray[np.float64]


@dataclass(frozen=True)
class AxisMapping:
    """Which device gyro axis (0, 1, 2 for X, Y, Z) drives image-x, image-y and roll, with signs."""

    axes: tuple[int, int, int]
    signs: tuple[int, int, int]


class Orientation:
    """Camera orientation as quaternions at the log's sample times (camera axes)."""

    def __init__(self, times_s: Array, quats: Array, holes: Array | None = None) -> None:
        self.times_s = times_s
        self.quats = quats
        self.holes = holes if holes is not None else np.zeros((0, 2))

    @classmethod
    def from_gyro(
        cls,
        log: GyroLog,
        mapping: AxisMapping,
        pitch: bool = True,
        gains: tuple[float, float, float] = (1.0, 1.0, 1.0),
    ) -> Orientation:
        """`gains` scale the turning that moves the picture side to side, up and down and in roll (the
        sync fit lowers an axis whose full correction would add shake the gyro cannot see)."""
        g = log.gyro
        mapped = np.stack([mapping.signs[k] * g[:, mapping.axes[k]] * gains[k] for k in range(3)], -1)
        # camera angular velocity (a, b, c): image dx = f*b, image dy = -f*a, roll = c
        rate = np.stack([-mapped[:, 1], mapped[:, 0], mapped[:, 2]], -1)
        if not pitch:  # leave up-and-down tilt alone (when vertical shake is really translation)
            rate[:, 0] = 0.0
        times = log.times_s
        steps = quat_from_rotvec(0.5 * (rate[1:] + rate[:-1]) * np.diff(times)[:, None])
        quats = np.zeros((len(times), 4))
        quats[0] = [1.0, 0.0, 0.0, 0.0]
        w, x, y, z = 1.0, 0.0, 0.0, 0.0
        # This recurrence is sequential. Scalar components avoid NumPy dispatch for each sample;
        # bounded lists keep that saving without boxing an entire recording's steps at once.
        for start in range(0, len(steps), 1024):
            for i, (sw, sx, sy, sz) in enumerate(steps[start : start + 1024].tolist(), start + 1):
                # C(t_i) = R(t_{i-1} -> t_i) C(t_{i-1}), normalised at every step as before.
                nw = sw * w - sx * x - sy * y - sz * z
                nx = sw * x + sx * w + sy * z - sz * y
                ny = sw * y - sx * z + sy * w + sz * x
                nz = sw * z + sx * y - sy * x + sz * w
                norm = math.sqrt(nw * nw + nx * nx + ny * ny + nz * nz)
                w, x, y, z = nw / norm, nx / norm, ny / norm, nz / norm
                quats[i] = (w, x, y, z)
        return cls(times, quats, log.holes())

    def at(self, t: npt.ArrayLike) -> Array:
        """Quaternions at any times (normalised linear interpolation, clamped to the log)."""
        times = np.clip(np.asarray(t, dtype=np.float64), self.times_s[0], self.times_s[-1])
        i = np.clip(np.searchsorted(self.times_s, times) - 1, 0, len(self.times_s) - 2)
        f = ((times - self.times_s[i]) / (self.times_s[i + 1] - self.times_s[i]))[..., None]
        a, b = self.quats[i], self.quats[i + 1]
        b = np.where((a * b).sum(-1, keepdims=True) < 0, -b, b)
        q = (1 - f) * a + f * b
        return np.asarray(q / np.linalg.norm(q, axis=-1, keepdims=True), dtype=np.float64)

    def covers(self, t: npt.ArrayLike) -> npt.NDArray[np.bool_]:
        times = np.asarray(t, dtype=np.float64)
        inside = (times >= self.times_s[0]) & (times <= self.times_s[-1])
        for start, end in self.holes:
            inside &= ~((times > start) & (times < end))
        return np.asarray(inside)
