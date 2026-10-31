from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from mcrawsome.motion.flow import (
    FLOW_WIDTH,
    Grey,
    frame_motion,
    measure_motion_raw,
    motion_image_raw,
    motion_image_video,
)


def _texture(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.random((270, 480)).astype(np.float32)
    big = cv2.GaussianBlur(noise, (0, 0), 3.0) + 0.3 * cv2.GaussianBlur(noise, (0, 0), 1.0)
    big = (big - big.min()) / (big.max() - big.min())
    return np.asarray(big * 255, dtype=np.uint8)


def _move(img: np.ndarray, dx: float, dy: float, roll: float) -> np.ndarray:
    c, s = np.cos(roll), np.sin(roll)
    cx, cy = img.shape[1] / 2, img.shape[0] / 2
    m = np.array([[c, -s, cx - c * cx + s * cy + dx], [s, c, cy - s * cx - c * cy + dy]], np.float32)
    return cv2.warpAffine(
        img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT
    )


@pytest.mark.parametrize(("dx", "dy", "roll"), [(3.0, -2.0, 0.0), (0.0, 0.0, 0.01), (-4.5, 1.5, -0.006)])
def test_recovers_known_motion(dx: float, dy: float, roll: float) -> None:
    prev = _texture()
    got = frame_motion(prev, _move(prev, dx, dy, roll), full_width=FLOW_WIDTH)
    assert got[0] == pytest.approx(dx, abs=0.3)
    assert got[1] == pytest.approx(dy, abs=0.3)
    assert got[2] == pytest.approx(roll, abs=np.radians(0.05))


def test_scales_to_full_resolution() -> None:
    prev = _texture()
    dx, _, _ = frame_motion(prev, _move(prev, 2.0, 0.0, 0.0), full_width=3840)
    assert dx == pytest.approx(2.0 * 3840 / FLOW_WIDTH, rel=0.1)


def test_a_strip_ignores_motion_elsewhere() -> None:
    prev = _texture()
    nxt = prev.copy()
    nxt[135:] = _move(prev, 6.0, 0.0, 0.0)[135:]  # only the bottom half moves
    top = frame_motion(prev, nxt, full_width=FLOW_WIDTH, rows=(0.0, 0.45))
    assert abs(top[0]) < 0.3


def test_strips_share_one_optical_flow_per_consecutive_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.motion import flow
    from mcrawsome.motion.sync import STRIPS

    prev = _texture()
    nxt = _move(prev, 3.0, -2.0, 0.006)
    expected = [frame_motion(prev, nxt, 3840, rows) for rows in STRIPS]
    create = cv2.DISOpticalFlow.create
    calls: list[int] = []

    def counted(preset: int) -> object:
        calls.append(preset)
        return create(preset)

    monkeypatch.setattr(cv2, "DISOpticalFlow", SimpleNamespace(create=counted))
    # The gap must not introduce a spurious pair or consume an optical-flow calculation.
    tracks = flow._tracks([0, 1, 3, 4], [prev, nxt, prev, nxt], 3840, STRIPS)
    assert len(calls) == 1
    for track, reference in zip(tracks, expected, strict=True):
        assert track.first.tolist() == [0, 3]
        np.testing.assert_array_equal(np.column_stack([track.dx, track.dy, track.roll]), [reference] * 2)


def test_motion_images() -> None:
    raw = np.full((216, 384), 64, np.uint16)
    raw[::2, 1::2] = 1000  # green sites on rows 0 for GBRG/GRBG-style layouts
    img = motion_image_raw(raw, "rggb", black=64.0)
    assert img.dtype == np.uint8
    assert img.shape[1] == FLOW_WIDTH
    video = motion_image_video(np.full((216, 384, 3), 0.4, np.float32))
    assert video.shape[1] == FLOW_WIDTH


def test_raw_motion_image_does_not_allocate_full_sensor_float_planes() -> None:
    import tracemalloc

    raw = np.full((768, 1024), 1000, np.uint16)
    tracemalloc.start()
    try:
        motion_image_raw(raw, "rggb", 64.0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < raw.nbytes * 3


@pytest.mark.parametrize("cfa", ["rggb", "bggr", "grbg", "gbrg"])
@pytest.mark.parametrize("black", [64.0, 63.7])
def test_green_only_motion_image_matches_full_sensor_arithmetic(cfa: str, black: float) -> None:
    from mcrawsome.motion.flow import _to_grey

    raw = np.random.default_rng(11).integers(0, 65536, (256, 384), dtype=np.uint16)
    saved = raw.copy()
    data = raw.astype(np.float32) - np.float32(black)
    gy, gx = (0, 0) if cfa[0] == "g" else (0, 1)
    expected = _to_grey(np.asarray(0.5 * (data[gy::2, gx::2] + data[gy + 1 :: 2, 1 - gx :: 2]), np.float32))
    np.testing.assert_array_equal(motion_image_raw(raw, cfa, black), expected)
    np.testing.assert_array_equal(raw, saved)


@pytest.mark.fixture
def test_measure_a_real_clip(fixture_12bit: Path) -> None:
    track = measure_motion_raw(fixture_12bit, [0, 1, 2], workers=2)
    assert track.first.tolist() == [0, 1]
    assert track.dx.shape == (2,)


def test_video_motion_uses_recorded_frames(
    rotated_video: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from mcrawsome.motion import flow
    from mcrawsome.output.ffmpeg import find_ffmpeg

    video, _ = rotated_video
    ffmpeg = find_ffmpeg()[0]
    decoded = subprocess.run(
        [str(ffmpeg), "-v", "error", "-noautorotate", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=480:288:flags=area,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    expected = np.frombuffer(decoded, np.uint8).reshape(288, 480)
    seen: list[int] = []

    def spy(
        prev: Grey, nxt: Grey, full_width: int, strips: flow.Strips, **kwargs: object
    ) -> list[tuple[float, float, float]]:
        np.testing.assert_allclose(prev, expected, atol=2)
        seen.append(full_width)
        return [(0.0, 0.0, 0.0)]

    monkeypatch.setattr(flow, "_frame_motions", spy)
    track = flow.measure_motion_video(ffmpeg, video, [0, 1, 2], 160, 96)
    assert seen == [160, 160]
    assert track.first.tolist() == [0, 1]


def test_the_strip_images_are_closed_before_the_pool_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure part way through settles the pending tasks while the pool is still there."""
    from collections.abc import Callable, Generator, Iterable, Iterator
    from contextlib import contextmanager

    from conftest import SYNTHETIC_STEP, write_mcraw

    from mcrawsome.motion import flow

    clip = write_mcraw(tmp_path / "c.mcraw", [i * SYNTHETIC_STEP for i in range(3)])
    events: list[str] = []

    @contextmanager
    def pool(workers: int) -> Iterator[None]:
        try:
            yield None
        finally:
            events.append("pool gone")

    def images(
        _pool: None, fn: Callable[[int], object], items: Iterable[int], window: int
    ) -> Generator[object]:
        try:
            for _item in items:
                yield np.zeros((4, 4), np.float32)
        finally:
            events.append("closed")

    def fails(*args: object, **kwargs: object) -> tuple[float, float, float]:
        raise RuntimeError("motion failed")

    monkeypatch.setattr(flow, "worker_pool", pool)
    monkeypatch.setattr(flow, "bounded_imap", images)
    monkeypatch.setattr(flow, "_frame_motions", fails)
    with pytest.raises(RuntimeError, match="motion failed"):
        flow.measure_strips_raw(clip, [0, 1, 2], 1, flow.WHOLE)
    assert events == ["closed", "pool gone"]


def test_reuses_flow_workspace_without_reusing_previous_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.motion import flow

    first = _texture(2)
    second = _texture(3)
    moved_first = _move(first, 3.0, -2.0, 0.006)
    moved_second = _move(second, -4.0, 1.0, -0.003)
    expected = [frame_motion(first, moved_first, 3840), frame_motion(second, moved_second, 3840)]
    create = cv2.DISOpticalFlow.create
    created: list[int] = []

    def counted(preset: int) -> object:
        created.append(preset)
        return create(preset)

    monkeypatch.setattr(cv2, "DISOpticalFlow", SimpleNamespace(create=counted))
    track = flow._tracks([0, 1, 3, 4], [first, moved_first, second, moved_second], 3840, flow.WHOLE)[0]
    assert len(created) == 1
    np.testing.assert_array_equal(np.column_stack([track.dx, track.dy, track.roll]), expected)
