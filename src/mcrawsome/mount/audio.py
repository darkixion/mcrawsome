"""Random-access aligned PCM, with the same timestamp rules as Clip.audio and aligned_track."""

from __future__ import annotations

import bisect
import os
import struct
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ItemType
from mcrawsome.io.timecode import Timecode, bext


@dataclass(frozen=True)
class Segment:
    start: int
    length: int
    offset: int


class WaveView:
    def __init__(
        self,
        clip: Clip,
        fd: int,
        frames: int,
        rate: float,
        tempo: float,
        start: int = 0,
        timecode: Timecode | None = None,
    ) -> None:
        """The sound under `frames` output frames from output frame `start`; with `timecode`, a Broadcast
        Wave `bext` chunk gives it the first frame's time of day."""
        self.fd = fd
        self.channels = clip.metadata.audio_channels or 1
        self.rate = clip.metadata.audio_sample_rate or 48000
        self.length = round(frames / rate * self.rate)
        self.begin = round(start / rate * self.rate)  # in output samples
        self.tempo = tempo if abs(tempo - 1.0) > 1e-6 else 1.0
        self.segments: list[Segment] = []
        first_ts: int | None = None
        end = 0
        video_ns = clip.timestamps_ns
        gap_limit = round(((int(video_ns[-1] - video_ns[0])) / 1e9 + 1 / clip.fps + 0.05) * self.rate)
        for ref in clip.audio_refs:
            kind, size = struct.unpack("<II", os.pread(fd, 8, ref.offset))
            if kind != ItemType.AUDIO_DATA:
                raise ValueError("invalid audio chunk")
            length = size // (2 * self.channels)
            following = os.pread(fd, 16, ref.offset + 8 + size)
            timestamp = None
            if len(following) == 16:
                kind, meta_size, stamp = struct.unpack("<IIq", following)
                if kind == ItemType.AUDIO_DATA_METADATA and meta_size >= 8 and stamp >= 0:
                    timestamp = stamp
            if first_ts is None:
                first_ts = timestamp if timestamp is not None else int(video_ns[0])
            start = round((timestamp - first_ts) * self.rate / 1e9) if timestamp is not None else end
            delta = start - end
            skip = 0
            if delta > gap_limit:
                start = end
            elif delta < -max(round(0.05 * self.rate), length):
                skip = min(-delta, length)
                length -= skip
                start = end
            elif delta <= max(round(0.05 * self.rate), length):
                start = end
            self.segments.append(Segment(start, length, ref.offset + 8 + skip * self.channels * 2))
            end = start + length
        self.starts = [segment.start for segment in self.segments]
        self.shift = round((int(video_ns[0]) - (first_ts or 0)) * self.rate / 1e9)
        count = self.length * self.channels * 2
        broadcast = (
            bext(timecode.time_reference(self.rate), timecode.start_local, clip.path.stem)
            if timecode is not None
            else b""
        )
        if count > 0xFFFFFFFF - 36 - len(broadcast):
            raise ValueError("mounted WAV exceeds the 4 GiB WAV limit; use a physical export")
        fmt = struct.pack(
            "<4sIHHIIHH",
            b"fmt ",
            16,
            1,
            self.channels,
            self.rate,
            self.rate * self.channels * 2,
            self.channels * 2,
            16,
        )
        self.header = (
            struct.pack("<4sI4s", b"RIFF", count + 36 + len(broadcast), b"WAVE")
            + broadcast
            + fmt
            + struct.pack("<4sI", b"data", count)
        )
        self.size = len(self.header) + count

    def _source(self, first: int, stop: int) -> npt.NDArray[np.int16]:
        samples = np.zeros((stop - first, self.channels), np.int16)
        index = max(0, bisect.bisect_right(self.starts, first) - 1)
        for i in range(index, len(self.segments)):
            segment = self.segments[i]
            if segment.start >= stop:
                break
            lo, hi = max(first, segment.start), min(stop, segment.start + segment.length)
            if lo < hi:
                offset = segment.offset + (lo - segment.start) * self.channels * 2
                data = os.pread(self.fd, (hi - lo) * self.channels * 2, offset)
                samples[lo - first : hi - first] = np.frombuffer(data, "<i2").reshape(-1, self.channels)
        return samples

    def read(self, offset: int, size: int) -> bytes:
        head = len(self.header)
        prefix = self.header[offset : offset + size] if offset < head else b""
        start = max(offset, head) - head
        stop = min(offset + size, self.size) - head
        if start >= stop:
            return prefix
        stride = self.channels * 2
        first, last = start // stride, -(-stop // stride)
        at = np.arange(first + self.begin, last + self.begin, dtype=np.float64) * self.tempo
        lo = np.floor(at).astype(np.int64)
        # aligned_track pads before resampling, including one sample beyond a drifting output.
        base = int(lo[0]) + self.shift
        end = int(lo[-1]) + self.shift + (2 if self.tempo != 1 else 1)
        source = self._source(base, end)
        if self.tempo == 1:
            pcm = source
        else:
            fraction = (at - lo)[:, None]
            lower = source[lo - lo[0]].astype(np.float64)
            upper = source[lo - lo[0] + 1].astype(np.float64)
            pcm = np.clip(np.round(lower + (upper - lower) * fraction), -32768, 32767).astype(np.int16)
        return prefix + pcm.astype("<i2").tobytes()[start - first * stride : stop - first * stride]
