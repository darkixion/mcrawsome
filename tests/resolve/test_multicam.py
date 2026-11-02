"""Multicamera layout: cameras, tracks and places in time, from synthetic clip facts only."""

import json
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest

from mcrawsome.io.capture import CaptureMetadata
from mcrawsome.io.timecode import Timecode
from mcrawsome.library.annotations import ClipAnnotations
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import DngInfo
from mcrawsome.resolve.bin_archive import Media
from mcrawsome.resolve.multicam import arrange, camera
from mcrawsome.resolve.timeline_archive import write_timeline

IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
INFO = DngInfo(
    256, 144, "rggb", (0, 0, 0, 0), 65535, IDENTITY, IDENTITY, None, None, None, None, 17, 21, None,
    (0.95, 1.0, 1.08), 1, "Synthetic", "Multicamera probe", None, 0.0, b"",
)  # fmt: skip
LONDON = "Europe/London"
PIXEL = CaptureMetadata(manufacturer="Google", camera_model="Pixel 10 Pro XL")
GALAXY = CaptureMetadata(manufacturer="samsung", camera_model="SM-S938B")


def at(hour: int, minute: int, second: float, zone: str = LONDON, day: int = 4) -> float:
    whole = int(second)
    local = datetime(2026, 10, day, hour, minute, whole, round((second - whole) * 1e6), tzinfo=ZoneInfo(zone))
    return local.timestamp()


def clip(
    stem: str,
    started: float | None,
    frames: int = 240,
    capture: CaptureMetadata | None = PIXEL,
    rate: float = 24.0,
    zone: str = LONDON,
    details: str = "",
) -> Media:
    clock = Timecode.at(started, rate, zone) if started is not None else None
    return Media(
        Path("/media") / stem, stem, frames, rate, INFO, 75000, 0, 48000 * frames // 24, 48000, 2,
        ClipAnnotations(camera=details) if details else None, capture,
        timecode=clock.text() if clock else "", clock=clock,
    )  # fmt: skip


def test_a_camera_is_its_details_camera_else_its_phone() -> None:
    assert camera(clip("a", 0, details=" A cam ")) == "A cam"
    assert camera(clip("a", 0)) == "Google Pixel 10 Pro XL"
    assert camera(clip("a", 0, capture=GALAXY)) == "samsung SM-S938B"
    named = CaptureMetadata(manufacturer="Google", camera_model="Google Pixel")
    assert camera(clip("a", 0, capture=named)) == "Google Pixel"
    assert camera(clip("a", 0, capture=CaptureMetadata(camera_model="Pixel"))) == "Pixel"
    assert camera(clip("a", 0, capture=None)) == "Camera"


def test_each_camera_has_a_track_and_each_clip_its_time_of_day() -> None:
    # The second phone starts three and a half seconds after the first and stops after it.
    first = clip("pixel", at(17, 41, 22.0), frames=422)
    second = clip("galaxy", at(17, 41, 25.5), frames=502, capture=GALAXY)
    layout = arrange([second, first])
    assert layout.names == ("Google Pixel 10 Pro XL", "samsung SM-S938B")
    assert layout.tracks == (1, 0)
    origin = (17 * 3600 + 41 * 60 + 22) * 24
    assert first.clock is not None
    assert first.clock.start_frames == origin
    assert layout.starts == (origin + 84, origin)
    assert layout.cameras == ("samsung SM-S938B", "Google Pixel 10 Pro XL")


def test_one_cameras_clips_share_a_track_unless_they_overlap() -> None:
    one, two, three = (
        clip(stem, at(10, 0, s), frames=24 * 10) for stem, s in (("a", 0), ("b", 10), ("c", 15))
    )
    layout = arrange([one, two, three])
    # a and b follow one another on one track; c overlaps b, so a second phone of that model filmed it.
    assert layout.tracks == (0, 0, 1)
    assert layout.names == ("Google Pixel 10 Pro XL", "Google Pixel 10 Pro XL 2")
    assert layout.cameras[2] == "Google Pixel 10 Pro XL 2"


def test_a_details_camera_keeps_clips_of_one_model_apart() -> None:
    a = clip("a", at(9, 0, 0), details="A")
    b = clip("b", at(9, 0, 30), details="B")
    c = clip("c", at(9, 5, 0), details="A")
    layout = arrange([a, b, c])
    assert layout.names == ("A", "B")
    assert layout.tracks == (0, 1, 0)


def test_clips_across_midnight_and_time_zones_keep_their_order_and_distance() -> None:
    late = clip("late", at(23, 59, 50), frames=24 * 30)
    early = clip("early", at(0, 0, 5, day=5), frames=24 * 10, capture=GALAXY)
    layout = arrange([early, late])
    assert layout.starts[1] == (23 * 3600 + 59 * 60 + 50) * 24
    assert layout.starts[0] - layout.starts[1] == 15 * 24  # past 24:00:00:00, not back at the start
    paris = clip("paris", at(10, 0, 1, zone="Europe/Paris"), capture=GALAXY, zone="Europe/Paris")
    london = clip("london", at(9, 0, 0))
    placed = arrange([paris, london])
    assert placed.starts == (placed.starts[1] + 24, 9 * 3600 * 24)  # one second later, in London's time


def test_clips_without_a_time_of_day_cannot_be_placed() -> None:
    with pytest.raises(ValueError, match="b and c have no time of day"):
        arrange([clip("a", at(9, 0, 0)), clip("b", None), clip("c", None)])
    with pytest.raises(ValueError, match="at least one clip"):
        arrange([])


def test_a_timeline_covers_less_than_a_day() -> None:
    first = clip("first", at(9, 0, 0))
    with pytest.raises(ValueError, match="less than a day"):
        arrange([first, replace(clip("next", at(9, 0, 0, day=5)), frames=24)])


def mounted(source: Path, folder: Path, zone: str | None) -> MountedSequence:
    """Virtual media as a mount serves it: headers and sparse space, no decoded pixels."""
    folder.mkdir()
    with Sequence(source, timecode=zone) as sequence:
        for name in sequence.names():
            header = (
                sequence.wave.header if name.endswith(".wav") and sequence.wave else sequence._header(0)[0]
            )
            with (folder / name).open("wb") as stream:
                stream.write(header)
                stream.truncate(sequence.size(name))
        return MountedSequence(
            id=uuid.uuid4().hex[:24], source=source, path=folder, stamp=sequence.stamp,
            frames=len(sequence.sources), active=True, serving=True, timecode=zone,
        )  # fmt: skip


def test_a_multicamera_send_puts_each_phone_on_its_own_tracks_at_its_time_of_day(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    from conftest import _DATA, SYNTHETIC_RECEIVED_MS, SYNTHETIC_STEP

    stamps = [i * SYNTHETIC_STEP for i in range(24)]
    pixel = make_mcraw(tmp_path / "pixel.mcraw", stamps)
    phone = json.loads((_DATA / "container_metadata_174122.json").read_text())
    phone["deviceSpecificProfile"] |= {"deviceModel": "galaxy s25 ultra", "cameraId": "0"}
    phone["extraData"]["postProcessSettings"]["metadata"] |= {
        "build.manufacturer": "samsung", "build.model": "SM-S938B", "build.device": "pa3q"
    }  # fmt: skip
    galaxy = make_mcraw(
        tmp_path / "galaxy.mcraw",
        stamps[:12],
        container={key: phone[key] for key in ("deviceSpecificProfile", "extraData")},
        frame_fields={"recvdTimestampMs": str(SYNTHETIC_RECEIVED_MS + 500)},
    )
    zone = "Europe/London"
    mounts = [mounted(galaxy, tmp_path / "galaxy", zone), mounted(pixel, tmp_path / "pixel", zone)]
    path = tmp_path / "multicamera.drt"
    assert write_timeline(path, mounts, name="Multicamera", multicam=True) == 0
    expected = {}
    for source in (pixel, galaxy):
        with Sequence(source, timecode=zone) as sequence:
            assert sequence.timecode is not None
            expected[source.stem] = sequence.timecode.start_frames
    assert expected["galaxy"] > expected["pixel"]
    with ZipFile(path) as archive:
        trees = [
            ET.fromstring(archive.read(name).decode().replace("ListMgt::", "ListMgt__"))
            for name in archive.namelist()
        ]
    container = trees[2]
    tracks = container.findall("VideoTrackVec/Element/Sm2TiTrack")
    assert [track.findtext("UserDefinedName") for track in tracks] == [
        "Google Pixel 10 Pro XL",
        "samsung SM-S938B",
    ]
    assert len(container.findall("AudioTrackVec/Element/Sm2TiTrack")) == 2
    placed = [
        [(item.findtext("Name"), int(item.findtext("Start") or "0")) for item in track.iter("Sm2TiVideoClip")]
        for track in tracks
    ]
    assert placed == [
        [("pixel-[000000-000023].dng", expected["pixel"])],
        [("galaxy-[000000-000011].dng", expected["galaxy"])],
    ]


def test_a_multicamera_send_needs_time_of_day_media(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    from conftest import SYNTHETIC_STEP

    source = make_mcraw(tmp_path / "zero.mcraw", [i * SYNTHETIC_STEP for i in range(3)])
    mount = mounted(source, tmp_path / "zero", None)
    with pytest.raises(ValueError, match="zero has no time of day"):
        write_timeline(tmp_path / "zero.drt", [mount], name="Zero", multicam=True)
    assert not (tmp_path / "zero.drt").exists()
