"""Each RAW frame's capture values, read from its metadata once per recording for every graph that needs them.

A frame's metadata is about 60 KiB, mostly its lens-shading map. json.loads makes Python objects of all of
it (0.8 s for 1,217 frames); pydantic's parser only of the fields named here. What is kept is small: the
values the graphs plot, for the last few recordings, never a shading map. The gyro graph needs only each
frame's timing, which skips the shading maps (about 0.1 s) unless Analytics has read the recording already.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcrawsome.analysis.capture_plot import capture_values
from mcrawsome.analysis.diagnostics import _check
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerError

KEPT = 4  # recordings


class _Timing(BaseModel):
    """The metadata fields read, as MotionCam wrote them: unvalidated, as json.loads gives them."""

    model_config = ConfigDict(extra="ignore")

    received_ms: Any = Field(None, alias="recvdTimestampMs")
    exposure: Any = Field(None, alias="exposureTime")


class _Fields(_Timing):
    iso: Any = None
    focus: Any = Field(None, alias="focusDistance")
    neutral: Any = Field(None, alias="asShotNeutral")
    black: Any = Field(None, alias="dynamicBlackLevel")
    white: Any = Field(None, alias="dynamicWhiteLevel")
    noise: Any = Field(None, alias="noiseProfile")
    shading: Any = Field(None, alias="lensShadingMap")
    shading_width: Any = Field(None, alias="lensShadingMapWidth")
    shading_height: Any = Field(None, alias="lensShadingMapHeight")


_KEYS = {name: field.alias or name for name, field in _Fields.model_fields.items()}


@dataclass(frozen=True)
class FrameTiming:
    received_ms: Any  # recvdTimestampMs, as written
    exposure: Any  # exposureTime in nanoseconds, as written


@dataclass(frozen=True)
class FrameValues(FrameTiming):
    values: dict[str, float | None]  # what Analytics plots (capture_values); shared, so copy before changing
    shading: bool  # a usable lens-shading map


Rows = list[FrameValues | None] | list[FrameTiming | None]
_tables: OrderedDict[tuple[object, ...], Rows] = OrderedDict()
_building: dict[tuple[object, ...], threading.Lock] = {}
_guard = threading.Lock()


def frame_values(
    clip: Clip,
    stop: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[FrameValues | None]:
    """Every frame's values, None for a frame whose metadata cannot be read. Read once per recording (by path
    and file stamp): a second graph, or two asking at once, read nothing again."""
    return _kept(clip, "values", lambda: _read(clip, _Fields, stop, progress))  # type: ignore[return-value]


def frame_timing(clip: Clip, stop: threading.Event | None = None) -> list[FrameTiming | None]:
    """Every frame's receive time and exposure, from Analytics' values when it has read the recording."""
    values = _kept(clip, "values")
    if values is not None:
        return list(values)
    return _kept(clip, "timing", lambda: _read(clip, _Timing, stop, None))  # type: ignore[return-value]


def _kept(clip: Clip, kind: str, build: Callable[[], Rows] | None = None) -> Rows | None:
    """The rows kept for this recording; without them, `build` once for all who ask at the same time."""
    stat = os.stat(clip.path)
    key = (str(clip.path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, kind)
    with _guard:
        kept = _tables.get(key)
        if kept is not None:
            _tables.move_to_end(key)
        if kept is not None or build is None:
            return kept
        building = _building.setdefault(key, threading.Lock())
    try:
        with building:
            with _guard:
                kept = _tables.get(key)
            if kept is not None:
                return kept
            rows = build()
            with _guard:
                _tables[key] = rows
                while len(_tables) > KEPT:
                    _tables.popitem(last=False)
            return rows
    finally:
        with _guard:
            _building.pop(key, None)


def _read(
    clip: Clip,
    model: type[_Timing],
    stop: threading.Event | None,
    progress: Callable[[int, int], None] | None,
) -> Rows:
    rows: list[Any] = []
    for index in range(clip.frame_count):
        _check(stop)
        if progress is not None and index % 32 == 0:
            progress(index, clip.frame_count)
        try:
            fields = model.model_validate_json(clip.frame_metadata_json(index))
        except (ValidationError, ValueError, OSError, ContainerError):
            rows.append(None)
            continue
        if not isinstance(fields, _Fields):
            rows.append(FrameTiming(fields.received_ms, fields.exposure))
            continue
        # The parsed values themselves, not model_dump's deep copy of each shading map.
        metadata = {_KEYS[name]: getattr(fields, name) for name in fields.model_fields_set}
        values = capture_values(metadata)
        rows.append(FrameValues(fields.received_ms, fields.exposure, values, "shading0" in values))
    _check(stop)
    clip.check_unchanged()
    return rows
