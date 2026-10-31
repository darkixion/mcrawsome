"""Motion MotionCam 5.1 records inside the .mcraw: gyro, accelerometer and OIS lens shifts."""

from collections.abc import Callable
from pathlib import Path

import numpy as np
from conftest import SYNTHETIC_STEP, recorded_motion

from mcrawsome.io.clip import Clip
from mcrawsome.io.container import MOTION_SAMPLE, ItemType, read_index
from mcrawsome.io.trim import trim


def _clip(make_mcraw: Callable[..., Path], path: Path, frames: int = 12, **motion: float) -> Path:
    stamps = [1_000_000_000 + i * SYNTHETIC_STEP for i in range(frames)]
    return make_mcraw(path, stamps, motion=recorded_motion(stamps[0], frames / 24, **motion))


def test_recorded_motion_is_read_in_time_order_from_its_indexes(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    path = _clip(make_mcraw, tmp_path / "motion.mcraw", ois_px=3.0)
    with path.open("rb") as stream:
        index = read_index(stream)
    assert not index.recovered
    assert len(index.gyro) > 1
    assert len(index.accelerometer) > 1
    assert len(index.ois) > 1
    with Clip(path) as clip:
        assert clip.has_recorded_motion
        motion = clip.recorded_motion()
    assert motion is not None
    assert motion.accelerometer is not None
    assert motion.ois is not None
    gyro = motion.gyro
    assert np.all(np.diff(gyro.times_ns) > 0)
    assert gyro.times_ns[0] == 500_000_000  # half a second before the first frame
    assert np.allclose(np.diff(gyro.times_ns), 2_500_000)  # 400 Hz
    assert gyro.values.shape == (len(gyro.times_ns), 3)
    assert np.allclose(motion.accelerometer.values[:, 0], 9.80665)
    assert motion.ois.values.shape == (len(motion.ois.times_ns), 2)
    assert np.abs(motion.ois.values[:, 0]).max() > 2.9


def test_a_clip_without_recorded_motion_has_none(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    path = make_mcraw(tmp_path / "plain.mcraw", [0, SYNTHETIC_STEP])
    with Clip(path) as clip:
        assert not clip.has_recorded_motion
        assert clip.recorded_motion() is None


def test_an_unfinished_recording_finds_its_motion_by_scanning(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    path = _clip(make_mcraw, tmp_path / "whole.mcraw")
    data = path.read_bytes()
    with path.open("rb") as stream:
        last_chunk = read_index(stream).accelerometer[-1]
    cut = tmp_path / "cut.mcraw"
    cut.write_bytes(data[: last_chunk + 20])  # stops inside the last accelerometer chunk, before any index
    with cut.open("rb") as stream:
        index = read_index(stream)
    assert index.recovered
    with Clip(cut) as clip:
        motion = clip.recorded_motion()
    assert motion is not None
    assert motion.accelerometer is not None
    with Clip(path) as clip:
        whole = clip.recorded_motion()
    assert whole is not None
    assert whole.accelerometer is not None
    assert motion.gyro.times_ns.tolist() == whole.gyro.times_ns.tolist()  # recorded before the cut
    assert 0 < len(motion.accelerometer.times_ns) < len(whole.accelerometer.times_ns)


def test_damaged_chunks_and_repeated_samples_are_left_out(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    samples = np.zeros(4, dtype=MOTION_SAMPLE)
    samples["t"] = [10, 20, 20, 30]  # a repeated time keeps its first sample
    samples["x"] = [1.0, 2.0, 9.0, np.nan]  # a reading that is not a number goes
    path = make_mcraw(
        tmp_path / "damaged.mcraw", [0, SYNTHETIC_STEP], motion=[(ItemType.GYRO_DATA, samples)] * 2
    )
    with path.open("rb") as stream:
        offsets = read_index(stream).gyro
    raw = bytearray(path.read_bytes())
    raw[offsets[1] + 12 : offsets[1] + 16] = (99).to_bytes(4, "little")  # the second chunk's count lies
    path.write_bytes(bytes(raw))
    with Clip(path) as clip:
        motion = clip.recorded_motion()
    assert motion is not None
    assert motion.gyro.times_ns.tolist() == [10, 20]
    assert motion.gyro.values[:, 0].tolist() == [1.0, 2.0]


def test_a_trim_keeps_the_motion_of_its_frames_and_half_a_second_either_side(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    path = _clip(make_mcraw, tmp_path / "long.mcraw", frames=48)
    trim(path, tmp_path / "short.mcraw", 12, 12)
    with Clip(tmp_path / "short.mcraw") as clip:
        first = int(clip.timestamps_ns[0])
        motion = clip.recorded_motion()
    assert motion is not None
    assert motion.ois is not None
    assert motion.accelerometer is not None
    end = first + 12 * SYNTHETIC_STEP
    for samples in (motion.gyro, motion.accelerometer, motion.ois):
        assert samples.times_ns[0] >= first - 500_000_000
        assert samples.times_ns[0] < first - 490_000_000
        assert samples.times_ns[-1] < end + 500_000_000
        assert samples.times_ns[-1] > end + 490_000_000
