from pathlib import Path

import pytest

from mcrawsome.pipeline.render import FrameJob, iter_frames
from mcrawsome.pipeline.thermal import ThermalGuard, find_cpu_sensor


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_the_package_thermal_zone_is_preferred(tmp_path: Path) -> None:
    _write(tmp_path / "class/thermal/thermal_zone0/type", "acpitz\n")
    _write(tmp_path / "class/thermal/thermal_zone0/temp", "30000\n")
    _write(tmp_path / "class/thermal/thermal_zone3/type", "x86_pkg_temp\n")
    wanted = _write(tmp_path / "class/thermal/thermal_zone3/temp", "50000\n")
    _write(tmp_path / "class/hwmon/hwmon1/name", "k10temp\n")
    _write(tmp_path / "class/hwmon/hwmon1/temp1_label", "Tctl\n")
    _write(tmp_path / "class/hwmon/hwmon1/temp1_input", "60000\n")
    assert find_cpu_sensor(tmp_path) == wanted


def test_coretemp_package_input_is_found(tmp_path: Path) -> None:
    _write(tmp_path / "class/hwmon/hwmon2/name", "coretemp\n")
    _write(tmp_path / "class/hwmon/hwmon2/temp1_label", "Core 0\n")
    _write(tmp_path / "class/hwmon/hwmon2/temp1_input", "40000\n")
    _write(tmp_path / "class/hwmon/hwmon2/temp2_label", "Package id 0\n")
    wanted = _write(tmp_path / "class/hwmon/hwmon2/temp2_input", "55000\n")
    assert find_cpu_sensor(tmp_path) == wanted


def test_k10temp_tctl_input_is_found(tmp_path: Path) -> None:
    _write(tmp_path / "class/hwmon/hwmon0/name", "nvme\n")
    _write(tmp_path / "class/hwmon/hwmon1/name", "k10temp\n")
    _write(tmp_path / "class/hwmon/hwmon1/temp1_label", "Tctl\n")
    wanted = _write(tmp_path / "class/hwmon/hwmon1/temp1_input", "61000\n")
    assert find_cpu_sensor(tmp_path) == wanted


def test_no_sensor_gives_none(tmp_path: Path) -> None:
    _write(tmp_path / "class/thermal/thermal_zone0/type", "acpitz\n")
    _write(tmp_path / "class/hwmon/hwmon0/name", "nvme\n")
    assert find_cpu_sensor(tmp_path) is None
    assert find_cpu_sensor(tmp_path / "missing") is None


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _guard(sensor: Path | None, clock: _Clock, limit: float = 85.0) -> ThermalGuard:
    return ThermalGuard(limit, sensor, clock=clock, sleep=clock.sleep)


def test_a_hot_cpu_is_waited_out_to_the_margin(tmp_path: Path) -> None:
    sensor = _write(tmp_path / "temp", "90000\n")
    clock = _Clock()
    readings = iter(["88000\n", "80000\n", "74000\n"])

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        sensor.write_text(next(readings))

    guard = ThermalGuard(85.0, sensor, clock=clock, sleep=sleep)
    guard.wait()
    assert clock.slept == [2.0, 2.0, 2.0]
    assert guard.pauses == 1
    assert guard.paused_s == pytest.approx(6.0)
    assert guard.notes() == ["paused once (6 s in all) to let the CPU cool below 85 °C"]
    guard.wait()  # 74 is below the limit: no more waiting
    assert guard.pauses == 1


def test_the_note_counts_pauses_and_minutes(tmp_path: Path) -> None:
    guard = ThermalGuard(85.0, _write(tmp_path / "temp", "50000\n"))
    guard.pauses, guard.paused_s = 4, 190.0
    assert guard.notes() == ["paused 4 times (3 min 10 s in all) to let the CPU cool below 85 °C"]


def test_below_the_limit_never_sleeps(tmp_path: Path) -> None:
    sensor = _write(tmp_path / "temp", "84900\n")
    clock = _Clock()
    guard = _guard(sensor, clock)
    for _ in range(3):
        guard.wait()
    assert clock.slept == []
    assert guard.pauses == 0
    assert guard.notes() == []


def test_without_a_sensor_it_never_sleeps_and_says_so() -> None:
    clock = _Clock()
    guard = _guard(None, clock)
    guard.wait()
    assert clock.slept == []
    assert guard.notes() == ["no CPU temperature sensor found; --thermal-limit has no effect"]


def test_a_sensor_that_stops_reading_is_treated_as_absent(tmp_path: Path) -> None:
    sensor = _write(tmp_path / "temp", "70000\n")
    clock = _Clock()
    guard = _guard(sensor, clock)
    guard.wait()
    sensor.unlink()
    guard.wait()
    guard.wait()
    assert clock.slept == []
    assert guard.notes() == ["no CPU temperature sensor found; --thermal-limit has no effect"]


class _Counting(ThermalGuard):
    def __init__(self) -> None:
        super().__init__(85.0, None)
        self.calls = 0

    def wait(self) -> None:
        self.calls += 1


@pytest.mark.fixture
@pytest.mark.slow
def test_a_guarded_render_waits_once_per_unit_and_changes_nothing(fixture_12bit: Path) -> None:
    from mcrawsome.io.clip import Clip
    from mcrawsome.pipeline.render import choose_neutral

    with Clip(fixture_12bit) as clip:
        neutral = choose_neutral(clip, "median")
    job = FrameJob(clip_path=fixture_12bit, exposure_stops=0.0, neutral=neutral)
    plain = list(iter_frames(job, frame_count=3, workers=2))
    guard = _Counting()
    guarded = list(iter_frames(job, frame_count=3, workers=2, guard=guard))
    assert guard.calls == 3
    assert len(guarded) == len(plain)
    assert all((a == b).all() for a, b in zip(plain, guarded, strict=True))
