from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.analysis.orientation import PREFIX_BYTES, _prefix, check, estimate
from mcrawsome.camera.profile import Axes
from mcrawsome.io.gcsv import read_gcsv
from mcrawsome.io.gyro import GyroLog

AXES = Axes(axes=(1, 0, 2), signs=(-1, -1, -1))


def gravity(up: tuple[float, float, float]) -> GyroLog:
    t = np.arange(0, 5, 0.01)
    camera = np.tile(np.array(up) * 9.80665, (len(t), 1))
    basis = np.eye(3)[list(AXES.axes)] * np.array(AXES.signs)[:, None]
    return GyroLog(t, np.zeros_like(camera), camera @ basis, 0, "XYZ")


@pytest.mark.parametrize(
    ("up", "suggested"), [((0, -1, 0), 0), ((-1, 0, 0), 90), ((0, 1, 0), 180), ((1, 0, 0), 270)]
)
@pytest.mark.parametrize("recorded", [0, 2, 3])
def test_known_gravity_and_recorded_flip_suggest_the_display_turn(
    up: tuple[float, float, float], suggested: int, recorded: int
) -> None:
    hint = estimate(gravity(up), AXES, recorded)
    assert hint is not None
    wanted = (suggested + (180 if recorded == 3 else 0)) % 360
    assert hint.suggested_rotation == wanted
    assert hint.appears_wrong == (wanted != 0)
    assert hint.confidence == 1
    corrected = estimate(gravity(up), AXES, recorded, wanted)
    assert corrected is not None
    assert not corrected.appears_wrong


@pytest.mark.parametrize(
    "condition",
    ["gyro-only", "fast-spin", "acceleration", "sky", "tumbling", "short", "nan", "gap", "unknown-axes"],
)
def test_ambiguous_sensor_data_never_claims_orientation_is_wrong(condition: str) -> None:
    log = gravity((-1, 0, 0))
    if condition == "gyro-only":
        log = replace(log, accel=None)
    elif condition == "fast-spin":
        log.gyro[:] = 1
    elif condition == "acceleration":
        assert log.accel is not None
        log.accel[:] *= 1.5
    elif condition == "sky":
        log = gravity((0, 0, 1))
    elif condition == "tumbling":
        assert log.accel is not None
        log.accel[::2] *= -1
    elif condition == "short":
        log = replace(log, times_s=log.times_s / 10)
    elif condition == "nan":
        assert log.accel is not None
        log.accel[0, 0] = np.nan
    elif condition == "gap":
        log.times_s[250:] += 1
    elif condition == "unknown-axes":
        log = replace(log, orientation="xYZ")
    assert estimate(log, AXES, 0) is None


def write_log(path: Path, accel: str = "0,980665000,0") -> None:
    path.write_text(
        "GYROFLOW IMU LOG\ntscale,0.01\ngscale,1\nascale,0.00000001\norientation,XYZ\n"
        "t,gx,gy,gz,ax,ay,az\n" + "".join(f"{i},0,0,0,{accel}\n" for i in range(1000))
    )


def test_log_prefix_is_bounded_cached_and_invalidated_by_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcrawsome.analysis.orientation as module

    path = tmp_path / "test.gcsv"
    write_log(path)
    reads = []
    real = read_gcsv

    def read(path: Path, *, max_bytes: int):  # type: ignore[no-untyped-def]
        reads.append(max_bytes)
        return real(path, max_bytes=max_bytes)

    monkeypatch.setattr(module, "read_gcsv", read)
    _prefix.cache_clear()
    hint = check(path, AXES, 0)
    assert hint is not None
    assert hint.appears_wrong
    assert check(path, AXES, 0, 90) is not None
    assert reads == [PREFIX_BYTES]
    write_log(path, "980665000,0,0")
    hint = check(path, AXES, 0)
    assert hint is not None
    assert not hint.appears_wrong
    assert reads == [PREFIX_BYTES, PREFIX_BYTES]
    path.write_text("bad log")
    assert check(path, AXES, 0) is None


def test_partial_prefix_line_is_never_parsed(tmp_path: Path) -> None:
    path = tmp_path / "test.gcsv"
    write_log(path)
    limit = path.read_bytes().index(b"100,") + 8
    log = read_gcsv(path, max_bytes=limit)
    assert len(log.times_s) == 100
