"""Multicamera timelines: which camera recorded each clip, and where each clip sits in time.

A camera is a phone, named by its maker and model, unless the clip's details name its Camera. MotionCam
records no serial number, so two phones of one model are told apart only when their recordings overlap: a
phone records one clip at a time, so a camera's overlapping clips take a track each. Every clip sits at the
time of day its first mounted frame was recorded, from its time-of-day timecode on the phone's clock, so
clips from different phones line up as closely as their clocks agree.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from mcrawsome.resolve.bin_archive import Media

DAY_S = 24 * 3600


@dataclass(frozen=True)
class Layout:
    """Each clip's track (from 0) and first frame on the timeline, counted from local midnight at the
    timeline's rate as the earliest clip's timecode counts; `names` are the tracks' cameras."""

    tracks: tuple[int, ...]
    starts: tuple[int, ...]
    names: tuple[str, ...]

    @property
    def cameras(self) -> tuple[str, ...]:
        """Each clip's camera, as its track is named."""
        return tuple(self.names[track] for track in self.tracks)


def camera(item: Media) -> str:
    """The camera a clip came from: its details' Camera, else the phone's maker and model."""
    if item.annotations is not None and item.annotations.camera.strip():
        return item.annotations.camera.strip()
    maker = (item.capture.manufacturer or "").strip() if item.capture else ""
    model = (item.capture.camera_model or "").strip() if item.capture else ""
    if maker and model and not model.casefold().startswith(maker.casefold()):
        return f"{maker} {model}"
    return model or maker or "Camera"


def _listed(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def arrange(media: Sequence[Media]) -> Layout:
    """Place clips at the same frame rate by the time of day they were recorded, one track per camera."""
    if not media:
        raise ValueError("A multicamera timeline needs at least one clip")
    untimed = [item.stem for item in media if item.clock is None]
    if untimed:
        raise ValueError(
            f"{_listed(untimed)} {'has' if len(untimed) == 1 else 'have'} no time of day, so "
            f"{'it' if len(untimed) == 1 else 'they'} cannot be placed on a multicamera timeline"
        )
    whole = round(media[0].rate)
    # Frames since 1970 count the same way as each clip's timecode counts from its local midnight (time zones
    # are whole seconds from UTC), and still order clips across midnight and between time zones.
    moments = [math.floor(item.clock.start_local.timestamp() * whole) for item in media if item.clock]
    first = min(range(len(media)), key=lambda index: moments[index])
    clock = media[first].clock
    assert clock is not None
    starts = [clock.start_frames + moment - moments[first] for moment in moments]
    span = max(start + item.frames for start, item in zip(starts, media, strict=True)) - starts[first]
    if span >= DAY_S * whole:
        raise ValueError(
            "A multicamera timeline covers less than a day; select the clips of one shoot and send again"
        )
    cameras = [camera(item) for item in media]
    order = sorted(range(len(media)), key=lambda index: (starts[index], cameras[index], media[index].stem))
    ends: dict[str, list[int]] = {}  # each camera's tracks, by the frame after their last clip so far
    lanes: list[tuple[str, int]] = []  # each track's camera and its place among that camera's tracks
    tracks = [0] * len(media)
    for index in order:
        busy = ends.setdefault(cameras[index], [])
        lane = next((number for number, end in enumerate(busy) if end <= starts[index]), len(busy))
        if lane == len(busy):
            busy.append(0)
            lanes.append((cameras[index], lane))
        busy[lane] = starts[index] + media[index].frames
        tracks[index] = lanes.index((cameras[index], lane))
    names = tuple(name if lane == 0 else f"{name} {lane + 1}" for name, lane in lanes)
    return Layout(tuple(tracks), tuple(starts), names)
