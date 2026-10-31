import wave
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.io.clip import AudioTrack
from mcrawsome.output.audio import aligned_track, write_wav


def test_write_wav(tmp_path: Path) -> None:
    track = AudioTrack(samples=np.zeros((480, 2), np.int16), sample_rate=48000, first_timestamp_ns=0)
    p = tmp_path / "a.wav"
    write_wav(p, track)
    with wave.open(str(p)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, 48000, 480)


@given(offset_ms=st.integers(-500, 500))
def test_aligned_track_starts_at_the_first_frame(offset_ms: int) -> None:
    rate = 48000
    samples = np.arange(rate, dtype=np.int16).reshape(-1, 1)
    first_frame_ns = 10_000_000_000
    track = AudioTrack(
        samples=samples, sample_rate=rate, first_timestamp_ns=first_frame_ns - offset_ms * 1_000_000
    )
    aligned = aligned_track(track, first_frame_ns, duration_s=0.5)
    assert aligned.first_timestamp_ns == first_frame_ns
    assert len(aligned.samples) == rate // 2
    shift = offset_ms * rate // 1000
    if offset_ms >= 0:  # audio started early: the first samples are trimmed
        assert aligned.samples[0, 0] == samples[shift, 0]
    else:  # audio started late: silence first
        assert not aligned.samples[: -shift - 1].any()


def _whole_array_reference(track: AudioTrack, length: int, tempo: float) -> np.ndarray:
    """The original whole-array resampler, as the exactness reference."""
    needed = int(np.ceil(length * tempo)) + 1
    samples = track.samples
    if len(samples) < needed:
        samples = np.concatenate(
            [samples, np.zeros((needed - len(samples), samples.shape[1]), samples.dtype)]
        )
    at, source = np.arange(length) * tempo, np.arange(len(samples))
    stretched = [np.interp(at, source, samples[:, c].astype(np.float64)) for c in range(samples.shape[1])]
    return np.clip(np.round(np.stack(stretched, axis=1)), -32768, 32767).astype(np.int16)


def _noise_track(frames: int, seed: int = 1) -> AudioTrack:
    rng = np.random.default_rng(seed)
    samples = rng.integers(-32768, 32768, size=(frames, 2), dtype=np.int16)
    return AudioTrack(samples=samples, sample_rate=1000, first_timestamp_ns=0)


@pytest.mark.parametrize("tempo", [0.998, 1.002])
def test_blockwise_resample_equals_whole_array(tempo: float, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.output import audio

    monkeypatch.setattr(audio, "_RESAMPLE_BLOCK", 997)
    track = _noise_track(10_000)
    out = aligned_track(track, 0, duration_s=9.5, tempo=tempo)
    assert len(out.samples) == 9500
    assert out.samples.dtype == np.int16
    assert np.array_equal(out.samples, _whole_array_reference(track, 9500, tempo))


def test_tempo_above_one_pads_a_short_track() -> None:
    track = _noise_track(1000)
    out = aligned_track(track, 0, duration_s=1.0, tempo=1.002)
    assert np.array_equal(out.samples, _whole_array_reference(track, 1000, 1.002))
    assert not out.samples[-1].any() or len(out.samples) == 1000


def test_resample_clips_at_the_int16_limits() -> None:
    samples = np.tile(np.array([[32767, -32768], [-32768, 32767]], np.int16), (500, 1))
    track = AudioTrack(samples=samples, sample_rate=1000, first_timestamp_ns=0)
    out = aligned_track(track, 0, duration_s=0.9, tempo=1.001)
    assert out.samples.min() >= -32768
    assert out.samples.max() <= 32767
    assert np.array_equal(out.samples, _whole_array_reference(track, 900, 1.001))


def test_resample_memory_is_bounded() -> None:
    import tracemalloc

    frames = 48_000 * 180
    track = AudioTrack(samples=np.zeros((frames, 2), np.int16), sample_rate=48_000, first_timestamp_ns=0)
    input_bytes = track.samples.nbytes
    tracemalloc.start()
    try:
        out = aligned_track(track, 0, duration_s=179.0, tempo=1.001)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(out.samples) == 179 * 48_000
    assert peak < 3.5 * input_bytes


@pytest.mark.parametrize("delay_seconds", [600, 86400])
@pytest.mark.parametrize("tempo", [1.0, 1.002])
def test_late_audio_allocations_are_bounded(delay_seconds: int, tempo: float) -> None:
    import tracemalloc

    rate = 100
    track = AudioTrack(np.ones((100, 2), np.int16), rate, delay_seconds * 1_000_000_000)
    tracemalloc.start()
    try:
        result = aligned_track(track, 0, 1.0, tempo)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.samples.shape == (rate, 2)
    assert not result.samples.any()
    assert result.first_timestamp_ns == 0
    assert peak < 64_000
