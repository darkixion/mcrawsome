"""Proxy descriptors never replace original timeline/gyro geometry or aligned audio."""

import struct
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from mcrawsome.io.clip import Clip
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence
from mcrawsome.recipe.raw import RawCalibration
from mcrawsome.resolve import gyro
from mcrawsome.resolve.bin_archive import Media, write_bin
from mcrawsome.resolve.bin_codec import decode_map, decode_wire, unwrap
from mcrawsome.resolve.timeline_archive import write_timeline


def mounted(source: Path, path: Path, size: str) -> MountedSequence:
    path.mkdir()
    settings = RawCalibration(mount_proxy=size)
    with Sequence(source, raw_settings=settings) as sequence:
        for name in sequence.names():
            header = (
                sequence.wave.header if name.endswith(".wav") and sequence.wave else sequence._header(0)[0]
            )
            with (path / name).open("wb") as stream:
                stream.write(header)
                stream.truncate(sequence.size(name))
        return MountedSequence(
            id=uuid.uuid4().hex[:24],
            source=source,
            path=path,
            stamp=sequence.stamp,
            raw_settings=settings,
            frames=len(sequence.sources),
            active=True,
            serving=True,
        )


def documents(path: Path) -> list[ET.Element]:
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        return [
            ET.fromstring(archive.read(n).decode().replace("ListMgt::", "ListMgt__"))
            for n in archive.namelist()
        ]


@pytest.mark.parametrize("timeline", [False, True])
@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("turn", [0, 90])
def test_linked_archive_preserves_original_geometry_audio_and_proxy_metadata(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeline: bool, separate: bool, turn: int
) -> None:
    original = mounted(tiny_raw, tmp_path / "full", "full")
    proxy = mounted(tiny_raw, tmp_path / "quarter", "quarter")
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("archive decoded RAW pixels"))
    output = tmp_path / ("linked.drt" if timeline else "linked.drb")
    writer = write_timeline if timeline else write_bin
    writer(output, [original], name="Linked", proxies=[proxy], separate_audio=separate, rotations=[turn])
    trees = documents(output)
    video = next(c for t in trees for c in t.iter("Sm2MpVideoClip"))
    info = video.find("Video/BtVideoInfo")
    assert info is not None
    original_geometry = decode_map(unwrap(bytes.fromhex(info.findtext("Geometry") or ""))[0])
    assert original_geometry["Resolution"].value == struct.pack(">QQ", 128, 64)
    fields = decode_wire(unwrap(bytes.fromhex(video.findtext("FieldsBlob") or ""))[0])
    value = next(f.value for f in fields if f.number == 2)
    assert isinstance(value, bytes)
    descriptor = decode_map(decode_map(value)["VIDEO_INFO"].value or b"")
    geometry = decode_map(descriptor["Geometry"].value or b"")
    assert geometry["Resolution"].value == struct.pack(">QQ", 32, 16)
    assert geometry["UniqueId"] != original_geometry["UniqueId"]
    clip = {f.number: f.value for f in decode_wire(unwrap(descriptor["Clip"].value or b"")[0])}
    assert clip[1] == str(proxy.path).encode()
    assert clip[2] == b"tiny-[000000-000002].dng"
    original_clip = {
        f.number: f.value for f in decode_wire(unwrap(bytes.fromhex(info.findtext("Clip") or ""))[0])
    }
    assert original_clip[1] == str(original.path).encode()
    radiometry = descriptor["Radiometry"].value or b""
    assert radiometry[:1] == b"\x80"
    properties = {f.number: f.value for f in decode_wire(radiometry[1:])}
    assert properties[1] == 312
    assert properties[5] == 32
    assert properties[19] == turn * 2
    assert video.findtext("AudioSource") == "AUDIO_SOURCE_CUSTOM"
    audio = [c for t in trees for c in t.iter("Sm2MpAudioClip")]
    assert len(audio) == int(separate)
    audio_info = next(c for t in trees for c in t.iter("BtAudioInfo"))
    audio_clip = {
        f.number: f.value for f in decode_wire(unwrap(bytes.fromhex(audio_info.findtext("Clip") or ""))[0])
    }
    assert (audio_clip[1], audio_clip[2]) == (str(original.path).encode(), b"tiny.wav")  # the original's WAV
    assert not list(tmp_path.glob("*.wav"))
    if not separate:
        assert video.find("EmbeddedAudioVec/Element/BtAudioInfo") is not None
    if timeline:
        ti = next(c for t in trees for c in t.iter("Sm2TiVideoClip"))
        assert ti.findtext("MediaFilePath") == str(original.path / "tiny-[000000-000002].dng")
        sequence = next(c for t in trees for c in t.iter("Sm2Sequence"))
        setup = decode_map(unwrap(bytes.fromhex(sequence.findtext("FieldsBlob") or ""))[0])[
            "SequenceSetup"
        ].value
        assert setup is not None
        assert struct.unpack_from(">I", setup, 44)[0] == (64 if turn else 128)
        assert struct.unpack_from(">I", setup, 52)[0] == (128 if turn else 64)


@pytest.mark.parametrize("timeline", [False, True])
@pytest.mark.parametrize("invalid", ["count", "source", "settings", "full-proxy"])
def test_invalid_proxy_pair_is_rejected_before_publication(
    tiny_raw: Path, tmp_path: Path, timeline: bool, invalid: str
) -> None:
    original = mounted(tiny_raw, tmp_path / "full", "full")
    proxy = mounted(tiny_raw, tmp_path / "quarter", "quarter")
    if invalid == "source":
        proxy.source = tmp_path / "different.mcraw"
    elif invalid == "settings":
        proxy.quad_bayer_remosaic = not original.quad_bayer_remosaic
    elif invalid == "full-proxy":
        proxy.raw_settings = original.raw_settings
    output = tmp_path / "refused.zip"
    writer = write_timeline if timeline else write_bin
    with pytest.raises(ValueError, match=r"proxy|Original"):
        writer(output, [original], name="Bad", proxies=[] if invalid == "count" else [proxy])
    assert not output.exists()
    assert not list(tmp_path.glob("*.wav"))


def test_gyro_preparation_receives_full_original_with_small_proxy(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = mounted(tiny_raw, tmp_path / "full", "full")
    proxy = mounted(tiny_raw, tmp_path / "quarter", "quarter")
    monkeypatch.setattr(gyro, "wants_stabilisation", lambda _: True)

    class PlanningReachedError(Exception):
        pass

    def prepare(mount: MountedSequence, media: Media, *_: Any, **__: Any) -> None:
        assert mount.path == original.path
        assert (media.info.width, media.info.height) == (128, 64)
        raise PlanningReachedError

    monkeypatch.setattr(gyro, "prepare", prepare)
    output = tmp_path / "full-gyro.drt"
    with pytest.raises(PlanningReachedError):
        write_timeline(output, [original], name="Full gyro", stabilise=True, proxies=[proxy])
    assert not output.exists()
    assert not output.with_name(output.name + ".gyro").exists()
