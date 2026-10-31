"""Keep a long render from running the CPU hot: wait for it to cool when it passes a limit."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

NO_SENSOR = "no CPU temperature sensor found; --thermal-limit has no effect"


def _text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def find_cpu_sensor(sys_root: Path = Path("/sys")) -> Path | None:
    """The file that holds the CPU package temperature in millidegrees: the `x86_pkg_temp` thermal zone,
    else the `coretemp` input labelled "Package id 0", else the `k10temp` input labelled "Tctl"."""
    for zone in sorted((sys_root / "class/thermal").glob("thermal_zone*")):
        if _text(zone / "type") == "x86_pkg_temp" and (zone / "temp").is_file():
            return zone / "temp"
    for chip, label in (("coretemp", "Package id 0"), ("k10temp", "Tctl")):
        for hwmon in sorted((sys_root / "class/hwmon").glob("hwmon*")):
            if _text(hwmon / "name") != chip:
                continue
            for labelled in sorted(hwmon.glob("temp*_label")):
                reading = labelled.with_name(labelled.name.removesuffix("_label") + "_input")
                if _text(labelled) == label and reading.is_file():
                    return reading
    return None


def read_celsius(sensor: Path) -> float | None:
    text = _text(sensor)
    try:
        return int(text) / 1000 if text is not None else None
    except ValueError:
        return None


def _duration(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60} min {whole % 60} s" if whole >= 60 else f"{whole} s"


class ThermalGuard:
    """`wait()` returns at once while the CPU is below `limit_c`; at or above it, it sleeps until the CPU
    is at or below `limit_c - margin_c`. A sensor that is missing, or stops reading, is treated as absent."""

    def __init__(
        self,
        limit_c: float,
        sensor: Path | None,
        margin_c: float = 10.0,
        poll_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limit_c = limit_c
        self.sensor = sensor
        self.margin_c = margin_c
        self.poll_s = poll_s
        self.clock = clock
        self.sleep = sleep
        self.pauses = 0
        self.paused_s = 0.0
        self._lost = sensor is None

    def _read(self) -> float | None:
        if self.sensor is None or self._lost:
            return None
        value = read_celsius(self.sensor)
        if value is None:
            self._lost = True
        return value

    def wait(self) -> None:
        temperature = self._read()
        if temperature is None or temperature < self.limit_c:
            return
        began = self.clock()
        while temperature is not None and temperature > self.limit_c - self.margin_c:
            self.sleep(self.poll_s)
            temperature = self._read()
        self.pauses += 1
        self.paused_s += self.clock() - began

    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.pauses:
            times = "once" if self.pauses == 1 else f"{self.pauses} times"
            notes.append(
                f"paused {times} ({_duration(self.paused_s)} in all) to let the CPU cool below "
                f"{self.limit_c:g} °C"
            )
        if self._lost:
            notes.append(NO_SENSOR)
        return notes
