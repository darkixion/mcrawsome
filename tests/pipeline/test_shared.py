import dataclasses
import io
import itertools
import os
import subprocess
import sys
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.pipeline import render, shared
from mcrawsome.pipeline.render import FrameJob, iter_frames, render_video_frame
from mcrawsome.pipeline.shared import FrameRing, VideoDecoder

W, H, N = 320, 180, 12


def _shm_names() -> set[str]:
    """This process's shared memory blocks. Other test runs and other programs create and remove their own
    blocks at any time, so a leak check must not see them."""
    ours = f"mcrawsome_{os.getpid()}_"
    return {p.name for p in Path("/dev/shm").iterdir() if p.name.startswith(ours)}


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic_Davinci_Davinci-WG.mkv"
    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=24",
         "-frames:v", str(N), "-pix_fmt", "yuv444p10le", "-c:v", "ffv1", str(path)],
        check=True,
    )  # fmt: skip
    return path


def _job(video: Path) -> FrameJob:
    return FrameJob(
        clip_path=video, exposure_stops=0.0, neutral=(1.0, 1.0, 1.0), source="video",
        input_encoding="davinci", size=(W, H), fps=24.0,
    )  # fmt: skip


def test_ring_slots_are_independent_and_cleaned_up() -> None:
    before = _shm_names()
    with FrameRing(slots=3, shape=(H, W, 3)) as ring:
        for slot in range(3):
            ring.view(slot)[:] = slot
        assert [int(ring.view(s).mean()) for s in range(3)] == [0, 1, 2]
        assert ring.name in _shm_names()
    assert _shm_names() <= before


def test_a_block_left_by_an_earlier_process_with_the_same_pid_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared, "_numbers", itertools.count())
    left = shared_memory.SharedMemory(name=f"mcrawsome_{os.getpid()}_0", create=True, size=16)
    try:
        with FrameRing(1, (4, 4, 3)) as ring:
            assert ring.name == f"mcrawsome_{os.getpid()}_1"
    finally:
        left.close()
        left.unlink()


_HOLD_A_RING = """
import sys

from mcrawsome.pipeline.shared import FrameRing

with FrameRing(1, (4, 4, 3)) as ring:
    print(ring.name, flush=True)
    sys.stdin.read()
"""


def test_blocks_made_by_other_processes_are_not_counted_as_ours() -> None:
    # Another mcrawsome process and another program each create a block while a leak check is under way.
    before = _shm_names()
    other = subprocess.Popen(
        [sys.executable, "-c", _HOLD_A_RING], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert other.stdout is not None
        theirs = other.stdout.readline().strip()
        stranger = shared_memory.SharedMemory(create=True, size=16)
        try:
            assert theirs.startswith(f"mcrawsome_{other.pid}_")
            assert (Path("/dev/shm") / theirs).exists()
            assert _shm_names() == before
        finally:
            stranger.close()
            stranger.unlink()
    finally:
        other.communicate("", timeout=60)
    assert other.returncode == 0
    assert not (Path("/dev/shm") / theirs).exists()


def test_decoder_reads_every_frame(video: Path) -> None:
    buffer = np.empty((H, W, 3), np.uint16)
    with VideoDecoder(find_ffmpeg()[0], video, (W, H)) as decoder:
        count = 0
        while decoder.read_into(buffer):
            count += 1
    assert count == N


class _Process:
    def __init__(self) -> None:
        self.stdout = io.BytesIO()

    def poll(self) -> int:
        return 0

    def wait(self) -> int:
        return 0


def test_a_gpu_device_decodes_with_vaapi_and_otherwise_on_the_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[list[str]] = []

    def start(command: list[str], **kwargs: object) -> _Process:
        started.append(command)
        return _Process()

    monkeypatch.setattr("mcrawsome.pipeline.shared.subprocess.Popen", start)
    with VideoDecoder(Path("ffmpeg"), Path("clip.mp4"), (W, H), device="/dev/dri/renderD128", start_s=1.0):
        pass
    with VideoDecoder(Path("ffmpeg"), Path("clip.mp4"), (W, H), start_s=1.0):
        pass
    gpu, cpu = started
    hardware = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]
    assert gpu[gpu.index("-hwaccel") : gpu.index("-i")] == [*hardware, "-ss", "1.000000"]
    assert [part for part in gpu if part not in hardware] == cpu
    assert "-hwaccel" not in cpu


def test_an_nvidia_gpu_decodes_with_nvdec_and_scales_on_the_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[list[str]] = []

    def start(command: list[str], **kwargs: object) -> _Process:
        started.append(command)
        return _Process()

    monkeypatch.setattr("mcrawsome.pipeline.shared.subprocess.Popen", start)
    with VideoDecoder(
        Path("ffmpeg"), Path("clip.mp4"), (W, H), device="cuda:0", codec="av1", hardware_scale=True
    ):
        pass
    [command] = started
    assert command[command.index("-hwaccel") : command.index("-i")] == [
        "-hwaccel", "cuda", "-hwaccel_device", "0", "-c:v", "av1",
    ]  # fmt: skip
    # FFmpeg downloads each frame itself; scaling stays the processor's, as the AppImage has no CUDA filters.
    assert "-hwaccel_output_format" not in command
    assert command[command.index("-vf") + 1].startswith("scale=")


def test_video_renders_ask_for_a_gpu_decoder_for_their_codec(
    video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str | None] = []

    def device(ffmpeg: Path, codec: str | None) -> None:
        asked.append(codec)

    monkeypatch.setattr(render, "decode_device", device)
    frames = list(iter_frames(dataclasses.replace(_job(video), video_codec="ffv1"), N, workers=2))
    assert len(frames) == N
    assert asked == ["ffv1"]


def test_a_decoder_started_part_way_gives_the_frame_at_that_time(synthetic_video: Path) -> None:
    """An H.264 file with B-frames: the seek lands on the frame asked for, not on a keyframe near it."""
    ffmpeg, size = find_ffmpeg()[0], (320, 180)
    frames = []
    with VideoDecoder(ffmpeg, synthetic_video, size) as decoder:
        frame = np.empty((180, 320, 3), np.uint16)
        while decoder.read_into(frame):
            frames.append(frame.copy())
    for index in (1, 7, 29):
        with VideoDecoder(ffmpeg, synthetic_video, size, start_s=(index - 0.5) / 24) as decoder:
            single = np.empty((180, 320, 3), np.uint16)
            assert decoder.read_into(single)
        np.testing.assert_array_equal(single, frames[index])


def test_video_frames_come_back_in_order_and_match_single_frames(video: Path) -> None:
    frames = list(iter_frames(_job(video), N, workers=2))
    assert len(frames) == N
    decoded = []
    with VideoDecoder(find_ffmpeg()[0], video, (W, H)) as decoder:
        buffer = np.empty((H, W, 3), np.uint16)
        while decoder.read_into(buffer):
            decoded.append(buffer.copy())
    for index in (0, 5, N - 1):
        np.testing.assert_array_equal(frames[index], render_video_frame(_job(video), decoded[index], index))


def test_selected_frames_only(video: Path) -> None:
    frames = list(iter_frames(_job(video), N, workers=2, indices=[2, 7]))
    assert len(frames) == 2


def test_no_shared_memory_is_left_after_a_failure(video: Path) -> None:
    before = _shm_names()
    job = dataclasses.replace(_job(video), plan_path=video.with_suffix(".missing.npz"))
    with pytest.raises(FileNotFoundError):
        list(iter_frames(job, N, workers=2))
    assert _shm_names() <= before


_WORKERS_READ_A_RING = """
import multiprocessing as mp

from mcrawsome.pipeline.shared import FrameRing, read_slot

if __name__ == "__main__":
    with FrameRing(2, (4, 4, 3)) as ring:
        ring.view(1)[:] = 7
        with mp.get_context("spawn").Pool(2) as pool:
            frames = pool.starmap(read_slot, [(ring.name, 1, (4, 4, 3))] * 4)
    assert all(int(f.max()) == 7 for f in frames)
"""


def test_workers_reading_a_ring_leave_the_resource_tracker_quiet(tmp_path: Path) -> None:
    # Spawned workers share the parent's resource tracker: a worker must not unregister the parent's
    # block, or the parent's unlink makes the tracker print a KeyError traceback (seen on real exports).
    script = tmp_path / "ring.py"
    script.write_text(_WORKERS_READ_A_RING, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert "leaked" not in result.stderr, result.stderr


LONG = 30


@pytest.fixture
def long_video(tmp_path: Path) -> Path:
    path = tmp_path / "long_Davinci_Davinci-WG.mp4"
    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=24",
         "-vf", "noise=alls=20:allf=t", "-frames:v", str(LONG), "-c:v", "mpeg4", "-q:v", "2", str(path)],
        check=True,
    )  # fmt: skip
    return path


def test_an_emit_range_outside_the_render_is_an_error(video: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        next(iter_frames(_job(video), N, workers=2, emit=(N - 2, 4)))


def test_a_clip_shorter_than_the_render_is_an_error(video: Path) -> None:
    job = dataclasses.replace(_job(video), first_frame=4, clip_frames=N)
    with pytest.raises(ValueError, match="clip"):
        next(iter_frames(job, N - 2, workers=2))


def test_float_ring_keeps_scene_linear_values() -> None:
    before = _shm_names()
    with FrameRing(2, (4, 8, 3), dtype=np.float32) as ring:
        assert ring.frame_bytes == 4 * 8 * 3 * 4
        ring.view(0)[:] = -0.125
        ring.view(1)[:] = 2.75
        assert ring.view(0).dtype == np.float32
        np.testing.assert_array_equal(ring.view(0), np.full((4, 8, 3), -0.125, np.float32))
        np.testing.assert_array_equal(ring.view(1), np.full((4, 8, 3), 2.75, np.float32))
    assert _shm_names() <= before
