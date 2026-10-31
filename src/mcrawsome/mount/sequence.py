"""Stable, read-only DNG and WAV byte ranges. No editor or filesystem dependency."""

from __future__ import annotations

import errno
import os
import re
from array import array
from collections import OrderedDict
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import TracebackType
from typing import Self

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.colour import with_colour_matrix
from mcrawsome.camera.vignette import VignetteProfile, apply_profile
from mcrawsome.develop.crop import CropBox, clip_crop, crop_metadata, crop_raw, find_black_edges
from mcrawsome.develop.raw import levels
from mcrawsome.io.clip import Clip, RawFrame
from mcrawsome.io.describe import RateChoice, frame_map, rate_map, target_rate
from mcrawsome.io.metadata import FrameMetadata
from mcrawsome.io.timecode import Timecode, clip_timecode
from mcrawsome.mount.audio import WaveView
from mcrawsome.mount.exposure import picture_gains
from mcrawsome.output import dng_log
from mcrawsome.output.dng import (
    SCALE_FACTORS,
    DngInfo,
    ShadingBaker,
    baked_white,
    camera_model,
    dng_info,
    exposure_gain,
    exposure_headroom,
    mount_bake_bits,
    reduce_cfa,
    reduced_info,
    tag_value_offset,
    uncompressed_header,
    with_camera_model,
)
from mcrawsome.output.ffmpeg import frame_rate
from mcrawsome.recipe.raw import RawCalibration

_DRIFT_MS = 20.0  # frame-rate conversion's drift limit: output.frame_rate_drift_ms's default


def sequence_timecode(clip: Clip, zone: str, rate: float, first: int) -> Timecode:
    """Time-of-day timecode for a sequence served from output frame `first` at `rate`."""
    return clip_timecode(clip, zone, rate, first / rate)


def mounted_rate(clip: Clip, choice: RateChoice) -> Fraction:
    """The rate a clip mounts at by its Mounted frame rate, known without mounting it."""
    return target_rate(choice, clip.timestamps_ns) or frame_rate(clip.fps)


def source_stamp(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


class Sequence:
    def __init__(
        self,
        source: Path,
        *,
        cache_bytes: int = 128 << 20,
        vignette_profile: VignetteProfile | None = None,
        raw_settings: RawCalibration | None = None,
        frame_range: tuple[int, int] | None = None,
        quad_bayer_remosaic: bool | None = None,
        colour_matrix: tuple[float, ...] | None = None,
        timecode: str | None = None,
    ) -> None:
        """`frame_range` (first output frame, frame count) serves only those frames, numbered from 0, and
        the sound under them. `timecode` (a time zone) gives the DNGs and WAV time-of-day timecode from the
        first served frame's capture time; None numbers them from zero."""
        self.source = source
        self.vignette_profile = vignette_profile
        self.raw_settings = raw_settings or RawCalibration()
        self.stamp = source_stamp(source)
        self.clip = Clip(
            source,
            quad_bayer_remosaic=quad_bayer_remosaic,
            cfa_layout=self.raw_settings.cfa_layout,
            cfa_pattern=self.raw_settings.cfa_pattern,
        )
        self.fd = -1
        if colour_matrix is not None:  # the DNGs' matrices, as camera.colour_matrix sets an export's
            self.clip.metadata = with_colour_matrix(self.clip.metadata, colour_matrix)
        try:
            first = apply_profile(self.clip.metadata, self.clip.frame_metadata(0), vignette_profile)
            self.baker: ShadingBaker | None = None
            self.raw_settings = self.raw_settings.serving(first)
            settings = self.raw_settings
            blacks, white = levels(first, settings.black_level, settings.white_level)
            if white <= max(blacks):
                raise ValueError("Mounted white level must be above every black level")
            self.crop: CropBox | None = None
            if settings.mount_crop == "auto":  # three frames, read once when mounting
                picks = sorted({0, self.clip.frame_count // 2, self.clip.frame_count - 1})
                blacks, white = levels(first, settings.black_level, settings.white_level)
                frames = [self.clip.raw_frame(i).data for i in picks]
                self.crop = find_black_edges(frames, float(np.mean(blacks)), white)
            elif settings.mount_crop != "none":
                blacks, white = levels(first, settings.black_level, settings.white_level)
                self.crop = clip_crop(self.clip, settings.mount_crop, float(np.mean(blacks)), white)
                assert self.crop is not None
                block = 2 * SCALE_FACTORS[settings.mount_proxy]
                if any(
                    value % block
                    for value in (self.crop.width, self.crop.height, self.crop.left, self.crop.top)
                ):
                    raise ValueError(
                        f"Custom mount crop size and position must be multiples of {block} for the selected "
                        "proxy so original and proxy framing agree"
                    )
            if self.crop is not None:
                first = crop_metadata(first, self.crop)
            baked, self.normalise = settings.mount_shading == "baked", settings.mount_normalise_exposure
            self.reference = first
            self.gains: list[float] | None = None
            if self.normalise == "picture":  # measured once, before the editor reads a frame
                self.gains = picture_gains(self.clip, source, self.stamp, settings, self.crop)
            if baked or self.normalise != "off":
                # Headroom from the first frame alone: nothing reads the whole clip before it is mounted.
                blacks, white = levels(first, settings.black_level, settings.white_level)
                mode = settings.lens_shading_mode
                bits = mount_bake_bits(first, settings.lens_shading, mode, blacks, white) if baked else 0
                if self.normalise == "recorded":
                    bits += 1  # a frame up to a stop darker than the first is brightened without clipping
                elif self.gains is not None:
                    bits += exposure_headroom(self.gains)
                while bits and baked_white(blacks, white, bits) > 65535:
                    bits -= 1
                self.baker = ShadingBaker(
                    self.clip.metadata.sensor_arrangement, settings.lens_shading, mode, bits, shading=baked
                )
            self.log_bits = self._log_bits(first)
            if settings.mount_scale and self.log_bits is not None:
                raise ValueError("Scaled values need a linear mount: log codes already use their whole range")
            self.sample = 1 if self.log_bits is not None and self.log_bits <= 8 else 2
            self.fd = os.open(source, os.O_RDONLY)
            self.rate = mounted_rate(self.clip, settings.mount_frame_rate)
            if settings.mount_frame_rate == "keep":  # dropped frames filled, the sound's tempo matched
                self.sources = frame_map(self.clip.timestamps_ns, 1e9 / self.clip.fps)
                self.tempo = float(self.rate) / self.clip.fps
            else:  # a constant rate on the recorded times, as output.frame_rate: the sound keeps its speed
                self.sources = rate_map(self.clip.timestamps_ns, 1e9 / float(self.rate), _DRIFT_MS * 1e6)
                self.tempo = 1.0
            self.first = 0
            if frame_range is not None:
                start, length = frame_range
                if not 0 <= start < len(self.sources) or length < 1:
                    raise ValueError("the mounted range is outside the recording")
                self.first, self.sources = start, self.sources[start : start + length]
            self.timecode = (
                sequence_timecode(self.clip, timecode, float(self.rate), self.first) if timecode else None
            )
            self.factor = SCALE_FACTORS[settings.mount_proxy]
            # A full-size first frame for editors that size a sequence from it; the rest stay proxies.
            self.full_first = self.factor > 1 and settings.mount_first_frame == "full"
            self.neutral = self.clip.frame_metadata(0).as_shot_neutral
            self.pattern = re.compile(re.escape(source.stem) + r"-(\d{6,})\.dng")
            self.headers: OrderedDict[tuple[int, int], tuple[bytes, int]] = OrderedDict()
            self.infos: OrderedDict[tuple[int, int], DngInfo] = OrderedDict()
            # Eight bytes per recorded frame, including clips with repeated CFR output frames.
            self.sizes = array("Q", [0]) * self.clip.frame_count
            self.layouts: OrderedDict[tuple[int, int], int] = OrderedDict()
            self.first_size = 0  # the full-size first frame's, when it differs from the others
            self.pixels: OrderedDict[tuple[int, int], npt.NDArray[np.uint8] | npt.NDArray[np.uint16]] = (
                OrderedDict()
            )
            self.limit = max(0, cache_bytes)
            self.cached_bytes = 0
            self.wave = (
                WaveView(
                    self.clip,
                    self.fd,
                    len(self.sources),
                    float(self.rate),
                    self.tempo,
                    self.first,
                    self.timecode,
                )
                if self.clip.audio_chunk_count and self.clip.metadata.audio_sample_rate
                else None
            )
        except Exception:
            self.close()
            raise

    def _log_bits(self, first: FrameMetadata) -> int | None:
        """The bits of the log codes, as the physical log export finds them (from the first frame's white
        level and the values' headroom), or None for linear values."""
        settings = self.raw_settings
        if settings.mount_transfer != "log":
            return None
        _, white = levels(first, settings.black_level, settings.white_level)
        sensor = round(white).bit_length()
        bits = dng_log.output_bits(sensor, self.baker.bits if self.baker else 0, settings.mount_reduce_bits)
        if bits < dng_log.MIN_BITS:
            raise ValueError(
                f"develop.mount_reduce_bits {settings.mount_reduce_bits} leaves {bits} bits of a "
                f"{sensor}-bit clip; log DNGs need at least {dng_log.MIN_BITS}"
            )
        return min(bits, 16)

    @property
    def modified_ns(self) -> int:
        """The source snapshot's modification time, shared by its virtual DNGs and WAV."""
        return self.stamp[3]

    def _check(self) -> None:
        try:
            if source_stamp(self.source) == self.stamp:
                return
        except OSError:
            pass
        raise OSError(errno.EIO, "source recording changed or is offline")

    def names(self) -> list[str]:
        names = [f"{self.source.stem}-{index:06d}.dng" for index in range(len(self.sources))]
        if self.wave is not None:
            names.append(f"{self.source.stem}.wav")
        return names

    def _number(self, name: str) -> int:
        """The served frame's number in this sequence."""
        match = self.pattern.fullmatch(name)
        if match is not None:
            index = int(match[1])
            if index < len(self.sources) and name == f"{self.source.stem}-{index:06d}.dng":
                return index
        raise FileNotFoundError(errno.ENOENT, name)

    def _index(self, name: str) -> int:
        """The recorded frame a served frame shows."""
        return self.sources[self._number(name)]

    def _factor(self, number: int) -> int:
        """The proxy factor of the served frame `number`."""
        return 1 if self.full_first and number == 0 else self.factor

    def first_info(self) -> DngInfo:
        """The first served frame's description: the size an editor lists the sequence at."""
        return self.frame_info(self.sources[0], self._factor(0))

    def frame_info(self, index: int, factor: int | None = None) -> DngInfo:
        factor = self.factor if factor is None else factor
        if (index, factor) in self.infos:
            self.infos.move_to_end((index, factor))
            return self.infos[index, factor]
        meta = apply_profile(self.clip.metadata, self.clip.frame_metadata(index), self.vignette_profile)
        if self.crop is not None:
            meta = crop_metadata(meta, self.crop)
        settings = self.raw_settings
        info = replace(
            dng_info(
                self.clip.metadata,
                meta,
                self.rate,
                0.0,
                settings.lens_shading,
                shading_mode=settings.lens_shading_mode,
                black_level=settings.black_level,
                white_level=settings.white_level,
                baked_bits=self.baker.bits if self.baker is not None and self.baker.shading else None,
                rotation=settings.rotation,
            ),
            as_shot_neutral=self.neutral,
        )
        if self.baker is not None:
            info = replace(info, white_level=self._white(meta))
            if not self.baker.shading:  # exposure only: the gain maps stay, the headroom is still made up
                info = replace(info, baseline_exposure=info.baseline_exposure + self.baker.bits)
        if settings.mount_scale:
            scale = 65535 / info.white_level
            b0, b1, b2, b3 = (min(65534.0, level * scale) for level in info.black_levels)
            info = replace(info, white_level=65535, black_levels=(b0, b1, b2, b3))
        if settings.mount_camera_model is not None or settings.mount_unique_model:
            device = self.clip.metadata.device_model or "MotionCam"
            names = camera_model(
                device, self.source.stem, settings.mount_camera_model, settings.mount_unique_model
            )
            info = with_camera_model(info, names)
        if factor > 1:
            block = 2 * factor  # whole Bayer blocks only, as the physical proxy export
            info = reduced_info(info, info.width // block * 2, info.height // block * 2, factor)
        if self.timecode is not None:  # a placeholder: read() writes each served frame's own address
            info = replace(info, timecodes=bytes(8))
        self.infos[index, factor] = info
        if len(self.infos) > 16:
            self.infos.popitem(last=False)
        return info

    def _header(self, index: int, factor: int | None = None) -> tuple[bytes, int]:
        factor = self.factor if factor is None else factor
        if (index, factor) not in self.headers:
            info = self.frame_info(index, factor)
            header = uncompressed_header(info, reduced=factor > 1, log_bits=self.log_bits)
            pixels = info.width * info.height * self.sample
            size = len(header) + pixels
            known = self.sizes[index] if factor == self.factor else self.first_size
            if known and known != size:
                raise ValueError("DNG header layout changed unexpectedly")
            if factor == self.factor:
                self.sizes[index] = size
            else:
                self.first_size = size
            self.headers[index, factor] = header, pixels
            if len(self.headers) > 16:
                self.headers.popitem(last=False)
        self.headers.move_to_end((index, factor))
        return self.headers[index, factor]

    def size(self, name: str) -> int:
        self._check()
        if self.wave is not None and name == f"{self.source.stem}.wav":
            return self.wave.size
        number = self._number(name)
        index = self.sources[number]
        if self._factor(number) != self.factor:
            if not self.first_size:
                self._header(index, 1)
            return self.first_size
        if not self.sizes[index]:
            info = self.frame_info(index)
            # Camera tags and their element counts are fixed for this sequence. Only the gain-map
            # byte count and noise-profile element count change the per-frame header layout.
            key = (len(info.gain_maps), len(info.noise_profile or ()))
            if key not in self.layouts:
                header = uncompressed_header(info, reduced=self.factor > 1, log_bits=self.log_bits)
                self.layouts[key] = len(header)
                if len(self.layouts) > 16:
                    self.layouts.popitem(last=False)
            self.layouts.move_to_end(key)
            self.sizes[index] = self.layouts[key] + info.width * info.height * self.sample
        return self.sizes[index]

    def read(self, name: str, offset: int, size: int) -> bytes | memoryview:
        self._check()
        if offset < 0 or size < 0:
            raise OSError(errno.EINVAL, "negative read range")
        # Bound temporary allocations even for non-filesystem callers.
        size = min(size, 1 << 20)
        if self.wave is not None and name == f"{self.source.stem}.wav":
            return self.wave.read(offset, size)
        number = self._number(name)
        index = self.sources[number]
        factor = self._factor(number)
        header, count = self._header(index, factor)
        end = min(offset + size, len(header) + count)
        if self.timecode is not None and offset < len(header):
            at = tag_value_offset(header, 51043)
            if at is not None and at < end and at + 8 > offset:
                header = header[:at] + self.timecode.address(number) + header[at + 8 :]
        prefix = header[offset:end] if offset < len(header) else b""
        start = max(offset, len(header))
        if start >= end:
            return prefix
        # Log codes are dithered by the frame's number in the recording, as the physical export numbers
        # its frames, so a repeated frame is kept once for each number it is served under.
        key = (index if self.log_bits is None else number, factor)
        if key in self.pixels:
            data = self.pixels[key]
            self.pixels.move_to_end(key)
        else:
            data = self._decode(index, factor)
            if self.log_bits is None:
                data = np.ascontiguousarray(data, dtype="<u2")
            else:
                info = self.frame_info(index, factor)
                codes = dng_log.encode(data, info.white_level, self.log_bits, self.first + number)
                data = np.ascontiguousarray(codes, dtype="u1" if self.sample == 1 else "<u2")
            if data.nbytes != count:
                raise OSError(errno.EIO, "decoded frame dimensions differ from its DNG header")
            while self.pixels and self.cached_bytes + data.nbytes > self.limit:
                _, old = self.pixels.popitem(last=False)
                self.cached_bytes -= old.nbytes
            if data.nbytes <= self.limit:
                self.pixels[key] = data
                self.cached_bytes += data.nbytes
        # FUSE copies this contiguous buffer directly into its reply. The read-only view
        # owns a reference to the array, so eviction or closing cannot invalidate a reply.
        pixels = memoryview(data).cast("B")[start - len(header) : end - len(header)].toreadonly()
        return prefix + pixels.tobytes() if prefix else pixels

    def _white(self, meta: FrameMetadata) -> int:
        """The white level of a frame's served values, before any scaling to 16 bits."""
        settings = self.raw_settings
        blacks, white = levels(meta, settings.black_level, settings.white_level)
        return self.baker.white(blacks, white) if self.baker is not None else round(white)

    def _decode(self, index: int, factor: int | None = None) -> npt.NDArray[np.uint16]:
        return self._pixels(self.clip.raw_frame(index), self.factor if factor is None else factor)

    def _gain(self, raw: RawFrame) -> float:
        if self.gains is not None:
            return self.gains[raw.index]
        return exposure_gain(self.reference, raw.metadata) if self.normalise == "recorded" else 1.0

    def _pixels(self, raw: RawFrame, factor: int) -> npt.NDArray[np.uint16]:
        """A frame's served values: the sensor's, cropped, with the lens shading baked in and reduced for a
        proxy, as each is chosen."""
        if self.crop is not None:
            raw = crop_raw(raw, self.crop)
        data = raw.data
        if self.baker is not None:
            meta = apply_profile(self.clip.metadata, raw.metadata, self.vignette_profile)
            settings = self.raw_settings
            data = self.baker.bake(
                data, meta, *levels(meta, settings.black_level, settings.white_level), self._gain(raw)
            )
        if self.raw_settings.mount_scale:
            meta = apply_profile(self.clip.metadata, raw.metadata, self.vignette_profile)
            scale = np.float32(65535 / self._white(meta))
            data = np.minimum(np.rint(data * scale), 65535).astype(np.uint16)
        if factor > 1:
            data = reduce_cfa(data, self.clip.metadata.sensor_arrangement, factor)
        return data

    def close(self) -> None:
        self.clip.close()
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
