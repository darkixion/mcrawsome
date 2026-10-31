"""Audio alignment to the first video frame, from the recording timestamps."""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np

from mcrawsome.io.clip import AudioTrack
from mcrawsome.io.timecode import Timecode, bext


def audio_offset_s(first_audio_ns: int, first_frame_ns: int) -> float:
    """Seconds of audio before the first frame (negative when the audio starts late)."""
    return (first_frame_ns - first_audio_ns) / 1e9


def write_wav(path: Path, track: AudioTrack, timecode: Timecode | None = None) -> None:
    """16-bit PCM; with `timecode`, a Broadcast Wave `bext` chunk gives the first frame's time of day."""
    if timecode is None:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(track.samples.shape[1])
            w.setsampwidth(2)
            w.setframerate(track.sample_rate)
            w.writeframes(track.samples.astype("<i2").tobytes())
        return
    channels, rate = track.samples.shape[1], track.sample_rate
    data = track.samples.astype("<i2").tobytes()
    broadcast = bext(timecode.time_reference(rate), timecode.start_local, path.stem)
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16)
    with path.open("wb") as out:
        out.write(struct.pack("<4sI4s", b"RIFF", 36 + len(broadcast) + len(data), b"WAVE"))
        out.write(broadcast + fmt + struct.pack("<4sI", b"data", len(data)))
        out.write(data)


_RESAMPLE_BLOCK = 1 << 20  # output samples per block, so extra memory is constant in clip length


def _resample(samples: np.ndarray, length: int, tempo: float) -> np.ndarray:
    """Linear-interpolate `length` output samples at positions n * tempo, one block at a time,
    each from only the source span it needs (identical to interpolating the whole array)."""
    out = np.empty((length, samples.shape[1]), np.int16)
    for start in range(0, length, _RESAMPLE_BLOCK):
        stop = min(start + _RESAMPLE_BLOCK, length)
        at = np.arange(start, stop) * tempo
        lo = int(at[0])
        hi = min(int(at[-1]) + 2, len(samples))
        source = np.arange(lo, hi)
        for c in range(samples.shape[1]):
            block = np.interp(at, source, samples[lo:hi, c].astype(np.float64))
            out[start:stop, c] = np.clip(np.round(block), -32768, 32767)
    return out


def aligned_track(
    track: AudioTrack, first_frame_ns: int, duration_s: float, tempo: float = 1.0
) -> AudioTrack:
    """The audio from the first frame for `duration_s`: early audio is trimmed, late audio gets
    leading silence, and the end is cut or padded to the video's length. `tempo` (the labelled frame
    rate over the measured one) plays the recording that much faster, as the video mux's atempo does;
    the change is at most 0.2%, so it is resampled (a pitch change too small to hear)."""
    rate = track.sample_rate
    length = round(duration_s * rate)
    needed = length if abs(tempo - 1.0) <= 1e-6 else int(np.ceil(length * tempo)) + 1
    shift = round(audio_offset_s(track.first_timestamp_ns, first_frame_ns) * rate)
    source = track.samples[max(shift, 0) : max(shift, 0) + needed]
    silence = min(max(-shift, 0), needed)
    if silence == 0 and len(source) >= needed:
        samples = source
    else:
        samples = np.empty((needed, track.samples.shape[1]), track.samples.dtype)
        for start in range(0, needed, _RESAMPLE_BLOCK):
            samples[start : min(start + _RESAMPLE_BLOCK, needed)] = 0
        count = min(len(source), needed - silence)
        samples[silence : silence + count] = source[:count]
    if needed != length:
        samples = _resample(samples, length, tempo)
    return AudioTrack(samples=samples[:length], sample_rate=rate, first_timestamp_ns=first_frame_ns)
