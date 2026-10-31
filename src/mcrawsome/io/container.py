"""Reading and writing the MotionCam .mcraw container (version 3).

The layout follows the official decoder (https://github.com/mirsadm/motioncam-decoder).
Frame payloads stay compressed here; `mcrawsome._raw.decode` decompresses them.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field, replace
from enum import IntEnum
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

import numpy as np
import numpy.typing as npt

IDENT = b"MOTION "
VERSION = 3
INDEX_MAGIC = 0x8A905612

MAX_METADATA_BYTES = 1 << 20  # real metadata items are a few KB

_ITEM = struct.Struct("<II")
_BUFFER_INDEX = struct.Struct("<iiq")
_OFFSET = struct.Struct("<qq")
_AUDIO_INDEX = struct.Struct("<qq")
_AUDIO_META = struct.Struct("<q")
_MOTION_HEADER = struct.Struct("<II")  # version, sample count; an index has the same header
MOTION_VERSION = 1
# Motion samples: the Android sensor timestamp (ns) and the reading. The motion sensors' x, y and z (gyro
# rad/s, accelerometer m/s2) are followed by four reserved bytes; the lens shift of optical image
# stabilisation is x and y in pixels of the sensor's pre-correction active array.
MOTION_SAMPLE = np.dtype([("t", "<i8"), ("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("reserved", "<u4")])
OIS_SAMPLE = np.dtype([("t", "<i8"), ("x", "<f4"), ("y", "<f4")])


class ItemType(IntEnum):
    BUFFER_INDEX = 0
    BUFFER_INDEX_DATA = 1
    BUFFER = 2
    METADATA = 3
    AUDIO_INDEX = 4
    AUDIO_DATA = 5
    AUDIO_DATA_METADATA = 6
    AUDIO_DATA_F32 = 7
    GYRO_INDEX = 8
    GYRO_DATA = 9
    OIS_INDEX = 10
    OIS_DATA = 11
    ACCELEROMETER_INDEX = 12
    ACCELEROMETER_DATA = 13


_SKIP_IN_TAIL = {
    ItemType.BUFFER,
    ItemType.METADATA,
    ItemType.AUDIO_DATA,
    ItemType.AUDIO_DATA_METADATA,
    ItemType.AUDIO_DATA_F32,
    ItemType.GYRO_INDEX,
    ItemType.GYRO_DATA,
    ItemType.OIS_INDEX,
    ItemType.OIS_DATA,
    ItemType.ACCELEROMETER_INDEX,
    ItemType.ACCELEROMETER_DATA,
}


class ContainerError(Exception):
    """The file is not a readable MotionCam container."""


class MissingIndexError(ContainerError):
    """The file has no frame index at its end (the recording was cut short)."""


@dataclass(frozen=True)
class FrameRef:
    timestamp_ns: int
    offset: int
    metadata_offset: int | None = field(default=None, compare=False)
    metadata_size: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AudioRef:
    offset: int
    timestamp_ns: int


@dataclass(frozen=True)
class ContainerIndex:
    metadata: dict[str, Any]
    frames: tuple[FrameRef, ...]
    audio: tuple[AudioRef, ...]
    recovered: bool = False
    index_damaged: bool = False  # recovered although an index was present
    # Where the chunks of motion recorded with the frames are (MotionCam 5.1 and later), in file order.
    gyro: tuple[int, ...] = ()
    accelerometer: tuple[int, ...] = ()
    ois: tuple[int, ...] = ()


def _read_exact(f: BinaryIO, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise ContainerError("unexpected end of file")
    return data


def _read_item(f: BinaryIO) -> tuple[int, int]:
    raw = f.read(_ITEM.size)
    if len(raw) != _ITEM.size:
        raise ContainerError("unexpected end of file")
    item_type, size = _ITEM.unpack(raw)
    return int(item_type), int(size)


def scan_items(f: BinaryIO, start: int, size: int) -> tuple[list[FrameRef], list[AudioRef]]:
    """Find complete frames and audio chunks, seeking over their payloads; stop at the first bad item."""
    frames: list[FrameRef] = []
    audio: list[AudioRef] = []
    buffer: int | None = None
    audio_offset: int | None = None
    offset = start
    while offset + _ITEM.size <= size:
        f.seek(offset)
        raw = f.read(_ITEM.size)
        if len(raw) != _ITEM.size:
            break
        item_type, length = _ITEM.unpack(raw)
        end = offset + _ITEM.size + length
        if end > size or item_type not in ItemType:
            break
        if item_type == ItemType.METADATA and buffer is not None:
            if length > MAX_METADATA_BYTES:
                break
            try:
                metadata = json.loads(_read_exact(f, length))
                timestamp = int(metadata["timestamp"])
            except (ContainerError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
                break
            frames.append(FrameRef(timestamp, buffer, offset, length))
        elif item_type == ItemType.AUDIO_DATA:
            audio.append(AudioRef(offset=offset, timestamp_ns=-1))
        elif item_type == ItemType.AUDIO_DATA_METADATA and audio_offset is not None:
            if length >= _AUDIO_META.size:
                (timestamp,) = _AUDIO_META.unpack(_read_exact(f, _AUDIO_META.size))
                audio[-1] = AudioRef(offset=audio_offset, timestamp_ns=timestamp)
        buffer = offset if item_type == ItemType.BUFFER else None
        audio_offset = offset if item_type == ItemType.AUDIO_DATA else None
        offset = end
    frames.sort(key=lambda frame: frame.timestamp_ns)
    return frames, audio


def read_index(f: BinaryIO) -> ContainerIndex:
    """Read the indexes, or scan complete items when the recording was not finalised."""
    f.seek(0)
    header = f.read(8)
    if len(header) != 8 or header[:7] != IDENT:
        raise ContainerError("not a MotionCam .mcraw file")
    if header[7] != VERSION:
        raise ContainerError(f"unsupported container version {header[7]}")
    item_type, size = _read_item(f)
    if item_type != ItemType.METADATA:
        raise ContainerError("missing container metadata")
    if size > MAX_METADATA_BYTES:
        raise ContainerError("container metadata is too large")
    try:
        metadata: dict[str, Any] = json.loads(_read_exact(f, size))
    except (ValueError, RecursionError):
        raise ContainerError("container metadata is not valid JSON") from None
    buffer_start = f.tell()
    file_size = f.seek(0, 2)
    try:
        frames, audio, motion = _read_indexes(f, buffer_start, file_size)
    except (ContainerError, OSError, ValueError, struct.error) as exc:
        frames, audio = scan_items(f, buffer_start, file_size)
        motion = _scan_motion(f, buffer_start, file_size)
        return ContainerIndex(
            metadata=metadata,
            frames=tuple(frames),
            audio=tuple(audio),
            recovered=True,
            index_damaged=not isinstance(exc, MissingIndexError),
            gyro=motion.get("gyro", ()),
            accelerometer=motion.get("accelerometer", ()),
            ois=motion.get("ois", ()),
        )
    return ContainerIndex(
        metadata=metadata,
        frames=tuple(frames),
        audio=tuple(audio),
        gyro=motion.get("gyro", ()),
        accelerometer=motion.get("accelerometer", ()),
        ois=motion.get("ois", ()),
    )


_MOTION_FIELDS = {
    ItemType.GYRO_DATA: "gyro",
    ItemType.ACCELEROMETER_DATA: "accelerometer",
    ItemType.OIS_DATA: "ois",
}
_MOTION_INDEXES = {
    ItemType.GYRO_INDEX: ItemType.GYRO_DATA,
    ItemType.ACCELEROMETER_INDEX: ItemType.ACCELEROMETER_DATA,
    ItemType.OIS_INDEX: ItemType.OIS_DATA,
}


def _scan_motion(f: BinaryIO, start: int, size: int) -> dict[str, tuple[int, ...]]:
    """The motion chunks of a recording without a usable index: all complete ones before a bad item."""
    found: dict[str, list[int]] = {name: [] for name in _MOTION_FIELDS.values()}
    offset = start
    while offset + _ITEM.size <= size:
        f.seek(offset)
        raw = f.read(_ITEM.size)
        if len(raw) != _ITEM.size:
            break
        item_type, length = _ITEM.unpack(raw)
        if offset + _ITEM.size + length > size or item_type not in ItemType:
            break
        if item_type in _MOTION_FIELDS:
            found[_MOTION_FIELDS[ItemType(item_type)]].append(offset)
        offset += _ITEM.size + length
    return {name: tuple(offsets) for name, offsets in found.items()}


def _motion_index(f: BinaryIO, size: int, buffer_start: int, data_offset: int) -> tuple[int, ...]:
    """The chunk offsets of a motion index: its header, then the file offset and first sample time of each."""
    if size < _MOTION_HEADER.size:
        raise ContainerError("corrupt motion index")
    version, count = _MOTION_HEADER.unpack(_read_exact(f, _MOTION_HEADER.size))
    if size != _MOTION_HEADER.size + _OFFSET.size * count:
        raise ContainerError("corrupt motion index")
    entries = _read_exact(f, _OFFSET.size * count)
    if version != MOTION_VERSION:
        return ()  # a later layout: the recording stays readable without its motion
    offsets = tuple(offset for offset, _ in _OFFSET.iter_unpack(entries))
    if any(not buffer_start <= offset < data_offset for offset in offsets):
        raise ContainerError("corrupt motion offset")
    return offsets


def _read_indexes(
    f: BinaryIO, buffer_start: int, file_size: int
) -> tuple[list[FrameRef], list[AudioRef], dict[str, tuple[int, ...]]]:
    tail = file_size - _ITEM.size - _BUFFER_INDEX.size
    if tail < buffer_start:
        raise MissingIndexError("missing frame index")
    f.seek(tail)
    item_type, size = _read_item(f)
    if item_type != ItemType.BUFFER_INDEX or size != _BUFFER_INDEX.size:
        raise MissingIndexError("missing frame index")
    magic, count, data_offset = _BUFFER_INDEX.unpack(_read_exact(f, _BUFFER_INDEX.size))
    if (
        (magic & 0xFFFFFFFF) != INDEX_MAGIC
        or count < 0
        or data_offset < buffer_start
        or data_offset + _OFFSET.size * count != tail
    ):
        raise ContainerError("corrupt frame index")
    f.seek(data_offset)  # MotionCam's offset table has no BUFFER_INDEX_DATA header.
    raw = _read_exact(f, _OFFSET.size * count)
    frames = sorted(
        (FrameRef(timestamp_ns=ts, offset=off) for off, ts in _OFFSET.iter_unpack(raw)),
        key=lambda fr: fr.timestamp_ns,
    )
    for i, frame in enumerate(frames):
        if not buffer_start <= frame.offset < data_offset:
            raise ContainerError("corrupt frame offset")
        f.seek(frame.offset)
        item_type, size = _read_item(f)
        if item_type != ItemType.BUFFER or f.tell() + size + _ITEM.size > tail:
            raise ContainerError("corrupt frame buffer")
        f.seek(size, 1)
        item_type, size = _read_item(f)
        if item_type != ItemType.METADATA or size > MAX_METADATA_BYTES or f.tell() + size > data_offset:
            raise ContainerError("corrupt frame metadata")
        frames[i] = replace(frame, metadata_offset=f.tell() - _ITEM.size, metadata_size=size)

    audio: list[AudioRef] = []
    motion: dict[str, tuple[int, ...]] = {}
    f.seek(max((fr.offset for fr in frames), default=buffer_start))
    while True:
        raw_item = f.read(_ITEM.size)
        if len(raw_item) != _ITEM.size:
            break
        item_type, size = _ITEM.unpack(raw_item)
        if f.tell() + size > file_size:
            raise ContainerError("corrupt container item")
        if item_type == ItemType.AUDIO_INDEX:
            if size < _AUDIO_INDEX.size:
                raise ContainerError("corrupt audio index")
            n_audio, _start_ms = _AUDIO_INDEX.unpack(_read_exact(f, _AUDIO_INDEX.size))
            if (
                n_audio < 0
                or size not in {_AUDIO_INDEX.size, _AUDIO_INDEX.size + _OFFSET.size * n_audio}
                or f.tell() + _OFFSET.size * n_audio > data_offset
            ):
                raise ContainerError("corrupt audio index")
            raw_audio = _read_exact(f, _OFFSET.size * n_audio)
            audio = [AudioRef(offset=o, timestamp_ns=t) for o, t in _OFFSET.iter_unpack(raw_audio)]
        elif item_type in _MOTION_INDEXES:
            name = _MOTION_FIELDS[_MOTION_INDEXES[ItemType(item_type)]]
            motion[name] = _motion_index(f, size, buffer_start, data_offset)
        elif item_type in _SKIP_IN_TAIL:
            f.seek(size, 1)
        else:
            break
    for chunk in audio:
        if not buffer_start <= chunk.offset < tail:
            raise ContainerError("corrupt audio offset")
        f.seek(chunk.offset)
        item_type, size = _read_item(f)
        if item_type != ItemType.AUDIO_DATA or f.tell() + size > tail:
            raise ContainerError("corrupt audio data")
    return frames, audio, motion


def read_frame_record(f: BinaryIO, offset: int) -> tuple[bytes, bytes]:
    """Return (compressed payload, frame metadata JSON bytes) for the frame at `offset`."""
    f.seek(offset)
    item_type, size = _read_item(f)
    if item_type != ItemType.BUFFER:
        raise ContainerError(f"expected a frame buffer at offset {offset}")
    payload = _read_exact(f, size)
    item_type, size = _read_item(f)
    if item_type != ItemType.METADATA:
        raise ContainerError(f"missing frame metadata at offset {offset}")
    return payload, _read_exact(f, size)


def read_frame_metadata(f: BinaryIO, offset: int, *, ref: FrameRef | None = None) -> bytes:
    """Frame metadata JSON bytes for the frame at `offset`, skipping the image data."""
    if ref is not None and ref.offset == offset and ref.metadata_offset is not None:
        # The index validation already located this header. Do not seek back into the
        # image buffer (which also causes unnecessary buffered read-ahead).
        f.seek(ref.metadata_offset)
        item_type, size = _read_item(f)
        if size != ref.metadata_size:
            raise ContainerError("frame metadata changed since the index was read")
    else:
        f.seek(offset)
        item_type, size = _read_item(f)
        if item_type != ItemType.BUFFER:
            raise ContainerError(f"expected a frame buffer at offset {offset}")
        f.seek(size, 1)
        item_type, size = _read_item(f)
    if item_type != ItemType.METADATA or size > MAX_METADATA_BYTES:
        raise ContainerError(f"invalid frame metadata at offset {offset}")
    return _read_exact(f, size)


def read_motion(f: BinaryIO, offsets: tuple[int, ...], item_type: ItemType) -> npt.NDArray[np.void]:
    """The samples of these motion chunks in time order, one per timestamp. Damaged chunks are left out, as
    MotionCam's own tools leave them, so a recording damaged in one place keeps the rest of its motion."""
    dtype = OIS_SAMPLE if item_type == ItemType.OIS_DATA else MOTION_SAMPLE
    chunks = []
    for offset in offsets:
        try:
            f.seek(offset)
            kind, size = _read_item(f)
            if kind != item_type or size < _MOTION_HEADER.size or size > MAX_METADATA_BYTES:
                continue
            version, count = _MOTION_HEADER.unpack(_read_exact(f, _MOTION_HEADER.size))
            if version != MOTION_VERSION or size != _MOTION_HEADER.size + dtype.itemsize * count:
                continue
            chunks.append(np.frombuffer(_read_exact(f, dtype.itemsize * count), dtype=dtype))
        except (ContainerError, OSError):
            continue
    samples = np.concatenate(chunks) if chunks else np.empty(0, dtype=dtype)
    names = [name for name in ("x", "y", "z") if name in dtype.names]  # type: ignore[operator]
    finite = np.logical_and.reduce([np.isfinite(samples[name]) for name in names])
    samples = np.sort(samples[finite], order="t", kind="stable")
    keep = np.ones(len(samples), dtype=bool)
    keep[1:] = np.diff(samples["t"]) > 0
    return samples[keep]


def read_audio_chunk(f: BinaryIO, offset: int) -> tuple[int | None, npt.NDArray[np.int16]]:
    """Return (timestamp in ns or None, interleaved int16 samples) for the chunk at `offset`."""
    f.seek(offset)
    item_type, size = _read_item(f)
    if item_type != ItemType.AUDIO_DATA:
        raise ContainerError(f"expected audio data at offset {offset}")
    samples = np.frombuffer(_read_exact(f, size - size % 2), dtype="<i2").astype(np.int16)
    f.seek(size % 2, 1)
    raw = f.read(_ITEM.size)
    timestamp: int | None = None
    if len(raw) == _ITEM.size:
        item_type, size = _ITEM.unpack(raw)
        if item_type == ItemType.AUDIO_DATA_METADATA and size >= _AUDIO_META.size:
            raw_metadata = f.read(_AUDIO_META.size)
            if len(raw_metadata) == _AUDIO_META.size:
                (timestamp,) = _AUDIO_META.unpack(raw_metadata)
    return timestamp, samples


class ContainerWriter:
    """Write a version 3 container. Used for trimming clips and for tests."""

    def __init__(self, path: Path, metadata: dict[str, Any]) -> None:
        self._f = path.open("wb")
        self._frames: list[FrameRef] = []
        self._audio: list[AudioRef] = []
        self._motion: dict[ItemType, list[tuple[int, int]]] = {}  # chunk offsets and first sample times
        self._f.write(IDENT + bytes([VERSION]))
        self._item(ItemType.METADATA, json.dumps(metadata).encode())

    def _item(self, item_type: ItemType, payload: bytes) -> int:
        offset = self._f.tell()
        self._f.write(_ITEM.pack(int(item_type), len(payload)))
        self._f.write(payload)
        return offset

    def add_frame(self, timestamp_ns: int, payload: bytes, metadata_json: bytes) -> None:
        offset = self._item(ItemType.BUFFER, payload)
        self._item(ItemType.METADATA, metadata_json)
        self._frames.append(FrameRef(timestamp_ns=timestamp_ns, offset=offset))

    def add_audio(self, samples: bytes, timestamp_ns: int | None) -> None:
        offset = self._item(ItemType.AUDIO_DATA, samples)
        if timestamp_ns is not None:
            self._item(ItemType.AUDIO_DATA_METADATA, _AUDIO_META.pack(timestamp_ns))
        self._audio.append(
            AudioRef(offset=offset, timestamp_ns=timestamp_ns if timestamp_ns is not None else -1)
        )

    def add_motion(self, item_type: ItemType, samples: npt.NDArray[np.void]) -> None:
        """A chunk of motion samples as MotionCam writes them (MOTION_SAMPLE; OIS_SAMPLE for lens shifts)."""
        data = np.asarray(samples, dtype=OIS_SAMPLE if item_type == ItemType.OIS_DATA else MOTION_SAMPLE)
        offset = self._item(item_type, _MOTION_HEADER.pack(MOTION_VERSION, len(data)) + data.tobytes())
        self._motion.setdefault(item_type, []).append((offset, int(data["t"][0]) if len(data) else 0))

    def close(self) -> None:
        if self._f.closed:
            return
        if self._audio:
            start_ms = max(self._audio[0].timestamp_ns, 0) // 1_000_000
            body = _AUDIO_INDEX.pack(len(self._audio), start_ms) + b"".join(
                _OFFSET.pack(a.offset, a.timestamp_ns) for a in self._audio
            )
            self._item(ItemType.AUDIO_INDEX, body)
        for index_type, data_type in _MOTION_INDEXES.items():
            if chunks := self._motion.get(data_type):
                entries = b"".join(_OFFSET.pack(offset, first) for offset, first in chunks)
                self._item(index_type, _MOTION_HEADER.pack(MOTION_VERSION, len(chunks)) + entries)
        index_body = b"".join(_OFFSET.pack(fr.offset, fr.timestamp_ns) for fr in self._frames)
        data_offset = self._item(ItemType.BUFFER_INDEX_DATA, index_body) + _ITEM.size
        self._item(
            ItemType.BUFFER_INDEX,
            _BUFFER_INDEX.pack(INDEX_MAGIC - (1 << 32), len(self._frames), data_offset),
        )
        self._f.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
