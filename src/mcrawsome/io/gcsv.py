"""Reader for Gyroflow .gcsv IMU logs (as written by MotionCam Pro)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from mcrawsome.io.gyro import GyroLog

__all__ = ["GcsvError", "GyroLog", "read_gcsv", "write_gcsv_slice"]

_IDENTIFIERS = ("GYROFLOW IMU LOG", "CAMERA IMU LOG")


class GcsvError(Exception):
    """The file is not a readable Gyroflow IMU log."""


def read_gcsv(path: Path, *, max_bytes: int | None = None) -> GyroLog:
    """Read a log, or a bounded complete-line prefix for lightweight diagnostics."""
    with path.open("rb") as source:
        raw = source.read() if max_bytes is None else source.read(max_bytes)
    if max_bytes is not None and len(raw) == max_bytes:
        raw = raw.rsplit(b"\n", 1)[0]  # never parse a partially read sample
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() not in _IDENTIFIERS:
        raise GcsvError(f"{path} is not a Gyroflow IMU log")
    header: dict[str, str] = {}
    columns: list[str] = []
    data_start = 0
    for i, line in enumerate(lines[1:], start=1):
        parts = [p.strip() for p in line.split(",")]
        if parts[0] == "t":
            columns = [p for p in parts if p]
            data_start = i + 1
            break
        if len(parts) >= 2:
            header[parts[0]] = parts[1]
    if not columns:
        raise GcsvError(f"{path} has no data header row")
    rows = [line.rstrip(",").split(",") for line in lines[data_start:] if line.strip()]
    data = np.array(rows, dtype=np.float64) if rows else np.empty((0, len(columns)), dtype=np.float64)
    col = {name: i for i, name in enumerate(columns)}
    try:
        tscale = float(header["tscale"])
        gscale = float(header["gscale"])
    except KeyError as missing:
        raise GcsvError(f"{path} lacks the {missing.args[0]} header") from None
    gyro = data[:, [col["gx"], col["gy"], col["gz"]]] * gscale
    accel = None
    if {"ax", "ay", "az"} <= col.keys():
        accel = data[:, [col["ax"], col["ay"], col["az"]]] * float(header.get("ascale", "1"))
    start = float(header["timestamp"]) / 1000.0 if "timestamp" in header else None
    return GyroLog(
        times_s=data[:, col["t"]] * tscale,
        gyro=gyro,
        accel=accel,
        start_epoch_s=start,
        orientation=header.get("orientation", "XYZ"),
        header=header,
        clock="epoch",
    )


def write_gcsv_slice(src: Path, dst: Path, start_s: float, end_s: float) -> None:
    """Write the rows covering a range on the log's clock, preserving the header and timestamps.
    Keep the nearest row on each side too, so interpolation covers the first and last frame."""
    log = read_gcsv(src)
    lines = src.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    data_start = next(i + 1 for i, line in enumerate(lines) if line.split(",")[0].strip() == "t")
    rows = [line for line in lines[data_start:] if line.strip()]
    first = max(0, int(np.searchsorted(log.times_s, start_s, side="right")) - 1)
    last = min(len(rows), int(np.searchsorted(log.times_s, end_s, side="left")) + 1)
    dst.write_text("".join(lines[:data_start] + rows[first:last]), encoding="utf-8")
