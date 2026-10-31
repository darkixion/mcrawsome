"""A lane-owned decoder: sequential and repeated pictures do not restart FFmpeg."""

from collections.abc import Callable
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome.io.directlog import DirectLogInfo
from mcrawsome.io.vaapi import decode_device
from mcrawsome.io.video_timing import presentation_times
from mcrawsome.output.ffmpeg import ffprobe_for, find_ffmpeg
from mcrawsome.pipeline.shared import VideoDecoder


class VideoFrames:
    def __init__(self, path: Path, info: DirectLogInfo, size: tuple[int, int], *, gpu: bool = True) -> None:
        self.path, self.info, self.size = path, info, size
        self.ffmpeg = find_ffmpeg()[0]
        self.times = presentation_times(ffprobe_for(self.ffmpeg), path)
        self.device = decode_device(self.ffmpeg, info.codec, preview=gpu)
        self.decoder: VideoDecoder | None = None
        self.index = -1
        self.pixels = np.empty((size[1], size[0], 3), np.uint16)

    def frame(self, source: int, check: Callable[[], None]) -> npt.NDArray[np.uint16]:
        check()
        if (
            self.decoder is None
            or source < self.index
            or source - self.index > max(24.0, float(self.info.fps))
        ):
            self.close()
            start = float(self.times[source - 1] + self.times[source]) / 2e9 if source > 0 else 0.0
            self.decoder = VideoDecoder(
                self.ffmpeg,
                self.path,
                self.size,
                matrix=self.info.tags.get("matrix", "bt709")
                if self.info.tags.get("matrix") in ("bt709", "bt470bg", "smpte170m", "bt2020nc")
                else "bt709",
                start_s=start,
                device=self.device,
                threads=8,
                codec=self.info.codec,
                hardware_scale=True,
            )
            self.index = source - 1
        while self.index < source:
            check()
            if not self.decoder.read_into(self.pixels):
                self.close()
                if self.device is not None:
                    # A small codec trial cannot guarantee this recording's profile or the
                    # driver's scaling support. Retry only this reader on the CPU.
                    self.device = None
                    return self.frame(source, check)
                raise ValueError(f"{self.path.name} ended before recorded frame {source}")
            self.index += 1
        check()
        return self.pixels

    def close(self) -> None:
        if self.decoder is not None:
            self.decoder.close()
            self.decoder = None
        self.index = -1
