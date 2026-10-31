"""Lists of clips for productions and other software: a camera report (CSV or Avid ALE) and the clips'
recorded positions as GPX. Built from the Library's facts, each clip's own details and the timecode its
mounted media carries under the current Settings; nothing is decoded and nothing is sent anywhere.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel

from mcrawsome.library.annotations import ClipAnnotations, annotations_for
from mcrawsome.library.scan import ClipSummary

log = logging.getLogger(__name__)
Format = Literal["csv", "ale"]
COLUMNS = (
    "Name", "Title", "Recorded", "Start", "End", "Duration", "Frames", "FPS", "Camera", "Camera label",
    "Lens", "Aperture", "ISO", "Shutter", "Shutter angle", "White balance", "Movement", "Shake blur",
    "Scene", "Shot", "Take", "Tag", "Rating", "Keywords", "Location", "Place", "Latitude", "Longitude",
    "Dropped frames", "Notes", "Source",
)  # fmt: skip
_TAGS = {"untagged": "", "good-take": "Good take", "rejected": "Rejected"}
_MOVEMENT = {"still": "Still", "pan": "Pan or tilt", "moving": "Moving", "handheld": "Handheld"}


class Report(BaseModel):
    name: str
    content: str


def _frames(clip: ClipSummary) -> int:
    """The frames its mounted media serves: dropped frames are filled."""
    return (clip.frame_count or 0) + (clip.missing_frames or 0)


def _marked(clip: ClipSummary) -> tuple[int, int]:
    """(first frame, frame count) the clip's mount serves: its In and Out marks, or the whole recording."""
    if clip.kind == "raw":
        from mcrawsome.io.container import ContainerError
        from mcrawsome.library.scan import clip_recipe
        from mcrawsome.recipe.clip_layer import read_layer
        from mcrawsome.server.routes.mounts import marked_range

        try:
            marked = marked_range(clip.path, clip_recipe(clip.path, read_layer(clip.path)))
        except (OSError, ValueError, ContainerError) as exc:
            log.info("Reporting all of %s: %s", clip.name, exc)
            marked = None
        if marked is not None:
            return marked
    return 0, max(1, _frames(clip))


def _timecodes(clip: ClipSummary) -> tuple[str, str, int]:
    """Start and end (last frame) timecode and the frame count, as the clip's mounted media would carry
    them now: from its In mark, at the time of day or from zero as Settings chooses."""
    from mcrawsome.io.timecode import Timecode, clip_timecode, video_start
    from mcrawsome.mount.manager import timecode_zone
    from mcrawsome.server.settings import load_settings

    rate = clip.fps or 24.0
    first, frames = _marked(clip)
    settings = load_settings()[0]
    code: Timecode | None = None
    try:
        zone = timecode_zone(settings.timecode, settings.timecode_zone, clip.path)
        if zone is not None and clip.kind == "raw":
            from mcrawsome.io.clip import Clip

            with Clip(clip.path, metadata_only=True) as recording:
                code = clip_timecode(recording, zone, rate, first / rate)
        elif zone is not None and (start := video_start(clip.path)) is not None:
            code = Timecode.at(start, rate, zone)
    except (OSError, ValueError) as exc:
        log.info("No time-of-day timecode for %s: %s", clip.name, exc)
    if code is None:
        code = Timecode.at(0, rate, "UTC")  # 00:00:00:00 at midnight UTC: timecode from zero
    return code.text(), code.text(frames - 1), frames


def _row(clip: ClipSummary) -> dict[str, str]:
    from mcrawsome.resolve.metadata import camera_rate, shutter_angle, shutter_speed

    details = annotations_for(clip.path, clip.id) if clip.id else ClipAnnotations()
    start, end, frames = _timecodes(clip)
    exposure = clip.exposure_time_ns / 1e9 if clip.exposure_time_ns else None
    rate = clip.measured_fps or clip.fps
    angle = shutter_angle(360 * rate * exposure) if rate and exposure else ""
    balance = ""
    if clip.as_shot_temperature:
        balance = f"{clip.as_shot_temperature:.0f} K"
        if clip.as_shot_tint is not None:
            balance += f", tint {clip.as_shot_tint:+.0f}"
    position = clip.position
    return {
        "Name": clip.name,
        "Title": details.title,
        "Recorded": clip.captured.astimezone().isoformat(timespec="seconds") if clip.captured else "",
        "Start": start,
        "End": end,
        "Duration": f"{frames / clip.fps:.2f}" if clip.fps else "",
        "Frames": str(frames),
        "FPS": camera_rate(rate) if rate else "",
        "Camera": clip.camera or "",
        "Camera label": details.camera,
        "Lens": f"{clip.focal_length_mm:.1f} mm" if clip.focal_length_mm else "",
        "Aperture": f"f/{clip.aperture:.1f}" if clip.aperture else "",
        "ISO": f"{clip.iso:.0f}" if clip.iso else "",
        "Shutter": shutter_speed(exposure) if exposure else "",
        "Shutter angle": angle,
        "White balance": balance,
        "Movement": _MOVEMENT.get(clip.movement or "", ""),
        "Shake blur": f"{clip.shake_blur_px:.0f} px" if clip.shake_blur_px is not None else "",
        "Scene": details.scene,
        "Shot": details.shot,
        "Take": details.take,
        "Tag": _TAGS[details.tag],
        "Rating": str(details.rating or clip.rating or ""),
        "Keywords": ", ".join(details.keywords),
        "Location": details.location,
        "Place": clip.place or "",
        "Latitude": f"{position[0]:.6f}" if position else "",
        "Longitude": f"{position[1]:.6f}" if position else "",
        "Dropped frames": str(clip.missing_frames or 0) if clip.kind == "raw" else "",
        "Notes": details.notes,
        "Source": str(clip.path),
    }


def camera_report(clips: list[ClipSummary], kind: Format) -> Report:
    """One row per clip, in the order given."""
    rows = [_row(clip) for clip in clips]
    if kind == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return Report(name="camera-report.csv", content=buffer.getvalue())
    return Report(name="camera-report.ale", content=_ale(clips, rows))


def _ale_rate(rate: float) -> str:
    """The clip's standard rate as ALE headers name it; otherwise the whole-number rate its timecode
    counts at (a 23.79 fps clip slowed by its shutter is 24)."""
    from mcrawsome.resolve.metadata import _FRAME_RATES

    near = min(_FRAME_RATES, key=lambda item: abs(item[0] - rate))
    return near[1] if abs(near[0] - rate) / near[0] < 0.002 else str(max(1, round(rate)))


def _cell(value: str) -> str:
    return " ".join(value.split())  # ALE cells hold no tabs or line breaks


def _ale(clips: list[ClipSummary], rows: list[dict[str, str]]) -> str:
    """Avid Log Exchange: one FPS for the file (the first clip's), standard columns where Avid has them."""
    rate = _ale_rate(clips[0].fps or 24.0) if clips else "24"
    tracks = ["VA1A2" if clip.kind == "raw" else "V" for clip in clips]
    columns = ["Name", "Tracks", "Start", "End", "Tape", "Scene", "Take", "Camera", "Comments", "Description"]
    extra = [
        name for name in COLUMNS if name not in {"Name", "Start", "End", "Scene", "Take", "Notes", "Title"}
    ]
    lines = [
        "Heading",
        "FIELD_DELIM\tTABS",
        "VIDEO_FORMAT\tCUSTOM",
        "AUDIO_FORMAT\t48khz",
        f"FPS\t{rate}",
        "",
    ]
    lines += ["Column", "\t".join(columns + extra), "", "Data"]
    for row, track in zip(rows, tracks, strict=True):
        standard = [
            row["Name"], track, row["Start"], row["End"], Path(row["Name"]).stem, row["Scene"], row["Take"],
            row["Camera label"] or row["Camera"], row["Notes"], row["Title"],
        ]  # fmt: skip
        lines.append("\t".join(_cell(value) for value in standard + [row[name] for name in extra]))
    return "\n".join(lines) + "\n"


def locations(clips: list[ClipSummary]) -> Report:
    """A GPX waypoint for each clip with a recorded fix, named by its title (or file name)."""
    points = []
    for clip in clips:
        if clip.position is None:
            continue
        details = annotations_for(clip.path, clip.id) if clip.id else ClipAnnotations()
        latitude, longitude = clip.position
        when = (
            f"<time>{clip.captured.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}</time>"
            if clip.captured
            else ""
        )
        about = " · ".join(part for part in (clip.place, details.location, clip.name) if part)
        points.append(
            f'  <wpt lat="{latitude:.7f}" lon="{longitude:.7f}">{when}'
            f"<name>{escape(details.title or clip.name)}</name><desc>{escape(about)}</desc></wpt>"
        )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<gpx version="1.1" creator={quoteattr("MCRAWsome")} xmlns="http://www.topografix.com/GPX/1/1">\n'
        + "".join(point + "\n" for point in points)
        + "</gpx>\n"
    )
    return Report(name="clip-locations.gpx", content=content)
