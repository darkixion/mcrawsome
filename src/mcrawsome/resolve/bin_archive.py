"""Native Resolve 21.1 bins for mounted CinemaDNG and PCM media.

The scaffold comes from our synthetic Resolve export. Source properties come only from metadata
and virtual file sizes. Building a bin never decodes a frame or writes a sequence to disk.
"""

from __future__ import annotations

import copy
import re
import struct
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from mcrawsome.errors import McError
from mcrawsome.io.capture import CaptureMetadata, capture_snapshot
from mcrawsome.library.annotations import ClipAnnotations, read_annotations
from mcrawsome.library.scan import ChangingError, clip_id
from mcrawsome.mount.limits import MAX_MOUNTS
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence, source_stamp
from mcrawsome.output.dng import DngInfo
from mcrawsome.resolve.bin_audio import sync_tracks, virtual_tracks
from mcrawsome.resolve.bin_codec import (
    MapField,
    decode_map,
    decode_wire,
    encode_map,
    patch_map,
    patch_wire,
    unwrap,
    wrap,
)
from mcrawsome.resolve.bin_geometry import orient_geometry
from mcrawsome.resolve.bin_name import patch_audio_name, patch_clip_name
from mcrawsome.resolve.bin_proxy import with_video_proxy
from mcrawsome.resolve.bin_white_balance import patch_white_balance, white_balance
from mcrawsome.resolve.geometry import matching_aspect
from mcrawsome.resolve.media_mounts import validate_proxy_pair
from mcrawsome.resolve.metadata import capture_fields, editorial_fields, keywords_field, patch_metadata
from mcrawsome.resolve.rotation import clip_rotation

_SEED = Path(__file__).parent / "data/bin_21_1"
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_STAMP = '<!--DbAppVer="21.1.1.0010" DbPrjVer="17"-->\n'


@dataclass(frozen=True)
class Media:
    folder: Path
    stem: str
    frames: int
    rate: float
    info: DngInfo
    frame_size: int
    modified_ns: int
    samples: int = 0
    sample_rate: int = 0
    channels: int = 0
    annotations: ClipAnnotations | None = None
    capture: CaptureMetadata | None = None
    keywords: tuple[str, ...] = ()  # measured keywords, added to the user's own on sends
    timecode: str = ""  # the first frame's time-of-day timecode, HH:MM:SS:FF; empty when counting from zero

    @property
    def video_name(self) -> str:
        return f"{self.stem}-[000000-{self.frames - 1:06d}].dng"

    @property
    def display_name(self) -> str:
        return (
            self.annotations.title if self.annotations and self.annotations.title.strip() else self.video_name
        )

    @property
    def audio_display_name(self) -> str:
        return (
            self.annotations.title
            if self.annotations and self.annotations.title.strip()
            else self.stem + ".wav"
        )


def audio_linked(media: Media) -> bool:
    return bool(media.channels)


def _sync_audio(video: ET.Element, audio: ET.Element, media: Media) -> None:
    node, raw, mode = _payload(video, "FieldsBlob")
    _, audio_raw, _ = _payload(audio, "FieldsBlob")
    node.text = wrap(sync_tracks(raw, audio_raw), mode).hex()
    _get(video, "AudioSource").text = "AUDIO_SOURCE_CUSTOM"
    _get(video, "VirtualAudioTracksBA").text = virtual_tracks(media.channels).hex()


def _annotate_audio(audio: ET.Element, media: Media) -> None:
    _get(audio, "Name").text = media.audio_display_name
    if media.annotations is not None:
        node, raw, mode = _payload(audio, "FieldsBlob")
        node.text = wrap(patch_audio_name(raw, media.annotations.title), mode).hex()


def mounted_media(mount: MountedSequence) -> Media:
    from mcrawsome.mount.readiness import readable_media

    if not mount.active or not mount.serving or not mount.source_available:
        raise ValueError("Sequence is unmounted or its source is offline; mount it again")
    if source_stamp(mount.source) != mount.stamp:
        raise ValueError("Source changed or is offline; mount the recording again")
    try:
        record = read_annotations(mount.source, clip_id(mount.source))
        annotations = record.values if record.version else None
    except (McError, ChangingError) as exc:
        raise ValueError(f"Could not read saved clip details: {exc}") from None
    with Sequence(
        mount.source,
        vignette_profile=mount.vignette_profile,
        colour_matrix=mount.colour_matrix,
        raw_settings=mount.raw_settings,
        frame_range=mount.frame_range,
        timecode=mount.timecode,
    ) as sequence:
        if sequence.stamp != mount.stamp or len(sequence.sources) != mount.frames:
            raise ValueError("Source changed since mounting")
        info = sequence.first_info()
        wave = sequence.wave
        if wave is not None and wave.channels not in (1, 2):
            raise ValueError("Native Resolve bins currently support mono or stereo WAV audio")
        size = readable_media(mount)
        if wave is not None and (mount.path / f"{mount.source.stem}.wav").stat().st_size != wave.size:
            raise ValueError("Mounted WAV properties changed; mount the recording again")
        return Media(
            mount.path,
            mount.source.stem,
            len(sequence.sources),
            float(sequence.rate),
            info,
            size,
            mount.stamp[3],
            wave.length if wave else 0,
            wave.rate if wave else 0,
            wave.channels if wave else 0,
            annotations,
            capture_snapshot(
                sequence.clip.metadata,
                sequence.clip.frame_metadata(0),
                measured_fps=sequence.clip.fps if sequence.clip.frame_count > 1 else None,
            ),
            _measured_keywords(mount.source),
            timecode=sequence.timecode.text() if sequence.timecode else "",
        )


def _measured_keywords(source: Path) -> tuple[str, ...]:
    """Measured keywords for a send, when Settings asks for them."""
    from mcrawsome.server.settings import load_settings

    if not load_settings()[0].resolve_keywords:
        return ()
    from mcrawsome.analysis.motion_insights import measured_keywords

    return measured_keywords(source)


def _annotate(video: ET.Element, lock: ET.Element, media: Media) -> None:
    # Apply after identifier remapping so UUID-shaped editorial text remains literal.
    _get(video, "Name").text = media.display_name
    updates = (
        capture_fields(
            media.capture,
            white_balance=white_balance(media.info),
            sample_rate=media.sample_rate,
            channels=media.channels,
        )
        if media.capture is not None
        else {}
    )
    if media.annotations is not None:
        node, raw, mode = _payload(video, "FieldsBlob")
        node.text = wrap(patch_clip_name(raw, media.annotations.title), mode).hex()
        updates.update(editorial_fields(media.annotations, media.capture, media.keywords))
    elif keywords := keywords_field([], media.keywords):
        updates[74] = keywords
    if updates:
        blob = _get(lock, "BtMetadataLockableBlob/FieldsBlob")
        blob.text = patch_metadata(bytes.fromhex(blob.text or ""), updates).hex()


def _binary(node: ET.Element) -> bool:
    return not len(node) and bool(node.text and len(node.text) % 2 == 0 and _HEX.fullmatch(node.text))


def _identifiers(trees: Iterable[ET.Element]) -> set[str]:
    found: set[str] = set()
    for tree in trees:
        found.update(_UUID.findall(ET.tostring(tree, encoding="unicode")))
        for node in tree.iter():
            if _binary(node):
                raw, _ = unwrap(bytes.fromhex(node.text or ""))
                found.update(_UUID.findall(raw.decode("ascii", errors="ignore")))
                for encoding in ("utf-16-be", "utf-16-le"):
                    for offset in (0, 1):
                        found.update(_UUID.findall(raw[offset:].decode(encoding, errors="ignore")))
    return {value for value in found if uuid.UUID(value).int}


def _fresh(trees: list[ET.Element], excluded: set[str] | None = None) -> dict[str, str]:
    mapping = {old: str(uuid.uuid4()) for old in _identifiers(trees) - (excluded or set())}
    for tree in trees:
        for node in tree.iter():
            for key, value in node.attrib.items():
                node.set(key, _UUID.sub(lambda m: mapping.get(m[0], m[0]), value))
            if _binary(node):
                raw, mode = unwrap(bytes.fromhex(node.text or ""))
                for old, new in mapping.items():
                    for encoding in ("ascii", "utf-16-be", "utf-16-le"):
                        raw = raw.replace(old.encode(encoding), new.encode(encoding))
                    for attribute in ("bytes", "bytes_le"):
                        raw = raw.replace(
                            getattr(uuid.UUID(old), attribute), getattr(uuid.UUID(new), attribute)
                        )
                node.text = wrap(raw, mode).hex()
            elif node.text:
                node.text = _UUID.sub(lambda m: mapping.get(m[0], m[0]), node.text)
    return mapping


def _get(tree: ET.Element, path: str) -> ET.Element:
    node = tree.find(path)
    if node is None:
        raise ValueError("Unsupported native Resolve bin scaffold")
    return node


def _payload(tree: ET.Element, path: str) -> tuple[ET.Element, bytes, tuple[int, int]]:
    node = _get(tree, path)
    raw, mode = unwrap(bytes.fromhex(node.text or ""))
    return node, raw, mode


def _map(tree: ET.Element, path: str, values: dict[str, bytes]) -> None:
    node, raw, mode = _payload(tree, path)
    node.text = wrap(patch_map(raw, values), mode).hex()


def _start_timecode(info: ET.Element, timecode: str) -> None:
    """Resolve takes a sent clip's Start TC from its Time record, where its own exports write a Timecode
    after UniqueId; without one it shows 00:00:00:00 whatever the DNGs' TimeCodes tags say."""
    node, raw, mode = _payload(info, "Time")
    entry = MapField(10, 0, timecode.encode("utf-16-be"))
    fields: dict[str, MapField] = {}
    for key, field in decode_map(raw).items():
        if key != "Timecode":
            fields[key] = field
        if key == "UniqueId":
            fields["Timecode"] = entry
    node.text = wrap(
        encode_map(fields if "Timecode" in fields else {"Timecode": entry, **fields}), mode
    ).hex()


def _audio_fields(raw: bytes, media: Media) -> bytes:
    # One track, with one or two channels referencing the same BtAudioInfo object.
    outer = decode_wire(raw)
    section = next(f.value for f in outer if f.number == 1)
    if not isinstance(section, bytes):
        raise ValueError("Unsupported native audio track")
    tracks = next(f.value for f in decode_wire(section) if f.number == 5)
    if not isinstance(tracks, bytes):
        raise ValueError("Unsupported native audio track")
    track_map = decode_map(tracks)
    track = track_map["0"].value or b""
    fields = decode_map(track)
    channels = decode_map(fields["ChannelVecBA"].value or b"")
    if media.channels == 1:
        channels.pop("1")
    track = patch_map(
        track,
        {
            "MediaExtents": struct.pack("<dd", 0, media.samples / media.sample_rate),
            "ChannelVecBA": encode_map(channels),
        },
    )
    tracks = patch_map(tracks, {"0": track})
    return patch_wire(raw, {1: patch_wire(section, {5: tracks})})


def _edit(clip: ET.Element, media: Media, *, audio: bool) -> None:
    _get(clip, "Name").text = media.stem + ".wav" if audio else media.video_name
    info = _get(clip, "EmbeddedAudioVec/Element/BtAudioInfo" if audio else "Video/BtVideoInfo")
    node, raw, mode = _payload(info, "Clip")
    # Native date labels use local wall time, matching the virtual files' source modification time.
    when = datetime.fromtimestamp(media.modified_ns // 1_000_000_000).astimezone()
    updates: dict[int, int | bytes] = {
        1: str(media.folder).encode(),
        2: (media.stem + ".wav" if audio else media.video_name).encode(),
        3: when.strftime("%a %b %e %H:%M:%S %Y").encode(),
        13: media.modified_ns // 1000,
    }
    if not audio:
        updates[6] = (media.stem + "-%06u.dng").encode()
    node.text = wrap(patch_wire(raw, updates), mode).hex()
    if audio:
        node, raw, mode = _payload(info, "TracksBA")
        tracks = decode_map(raw)
        tracks["0"] = MapField(
            12,
            0,
            patch_map(
                tracks["0"].value or b"",
                {
                    "Duration": struct.pack(">q", media.samples),
                    "SampleRate": struct.pack(">I", media.sample_rate),
                    "NumChannels": struct.pack(">i", media.channels),
                },
            ),
        )
        node.text = wrap(encode_map(tracks), mode).hex()
        node, raw, mode = _payload(clip, "FieldsBlob")
        node.text = wrap(_audio_fields(raw, media), mode).hex()
    else:
        _map(
            info,
            "Time",
            {
                "StartFrame": struct.pack(">i", 0),
                "NumFrames": struct.pack(">i", media.frames),
                "FrameRate": struct.pack("<dd", media.rate, 0),
            },
        )
        if media.timecode:
            _start_timecode(info, media.timecode)
        node, raw, mode = _payload(info, "Geometry")
        raw = patch_map(
            raw,
            {
                "Resolution": struct.pack(">QQ", media.info.width, media.info.height),
                "FrameSize": struct.pack(">i", media.frame_size),
            },
        )
        node.text = wrap(orient_geometry(raw, media.info.orientation), mode).hex()
        node, raw, mode = _payload(info, "Radiometry")
        node.text = wrap(patch_white_balance(raw, media.info), mode).hex()


def proxy_media(
    mounts: list[MountedSequence],
    media: list[Media],
    proxies: list[MountedSequence | None] | None,
) -> list[Media | None]:
    if proxies is None:
        return [None] * len(mounts)
    if len(proxies) != len(mounts):
        raise ValueError("Expected one optional proxy per original clip")
    result: list[Media | None] = []
    for original, item, proxy in zip(mounts, media, proxies, strict=True):
        if proxy is None:
            result.append(None)
            continue
        validate_proxy_pair(original, proxy)
        smaller = mounted_media(proxy)
        size = item.info.width, item.info.height
        proxy_size = smaller.info.width, smaller.info.height
        if (
            item.rate != smaller.rate
            or item.frames != smaller.frames
            or item.info.orientation != smaller.info.orientation
            or not matching_aspect(size, proxy_size)
            or any(p > full for p, full in zip(proxy_size, size, strict=True))
            or proxy_size == size
        ):
            raise ValueError(
                "Proxy must have matching timing, orientation and aspect at a smaller resolution"
            )
        result.append(smaller)
    return result


def attach_proxy(video: ET.Element, proxy: Media) -> None:
    # Clone only for constructing independently identified source metadata. The
    # original owner, paths, full geometry and audio are never replaced.
    template = copy.deepcopy(video)
    _fresh([template])
    _edit(template, proxy, audio=False)
    node, raw, mode = _payload(video, "FieldsBlob")
    node.text = wrap(with_video_proxy(raw, _get(template, "Video/BtVideoInfo")), mode).hex()


def _embed_audio(video: ET.Element, audio: ET.Element) -> None:
    # Owner-tested Resolve 21.1: retain CUSTOM/Origin=1 and own the descriptor on the video.
    # EMBEDDED/default origin produced silent playback. The descriptor keeps the mount's own WAV:
    # Resolve holds a loaded clip's audio open, so the media shows as in use for as long as it does.
    _get(video, "EmbeddedAudioVec").append(copy.deepcopy(_get(audio, "EmbeddedAudioVec/Element")))


def write_bin(
    path: Path,
    mounts: list[MountedSequence],
    *,
    name: str,
    rotations: list[int] | None = None,
    separate_audio: bool = False,
    link_audio: bool = True,
    proxies: list[MountedSequence | None] | None = None,
) -> None:
    """Write one native bin atomically, refusing unverified media before creating the archive."""
    if not 1 <= len(mounts) <= MAX_MOUNTS or not name.strip():
        raise ValueError(f"Select between 1 and {MAX_MOUNTS} mounted clips")
    if rotations is not None and (
        len(rotations) != len(mounts) or any(turn not in (0, 90, 180, 270) for turn in rotations)
    ):
        raise ValueError("Expected one clockwise quarter-turn display rotation per bin clip")
    if rotations is not None and any(mount.raw_settings.rotation for mount in mounts):
        raise ValueError("Clip Attributes rotation requires recorded-orientation mounted media")
    media = [mounted_media(mount) for mount in mounts]
    proxy_items = proxy_media(mounts, media, proxies)
    trees = [
        ET.fromstring((_SEED / filename).read_text().replace("ListMgt::", "ListMgt__"))
        for filename in ("project.xml", "master.xml", "bin.xml")
    ]
    _fresh(trees)
    project, master, folder = trees
    _get(project, "ProjectName").text = name
    _get(folder, "Name").text = name
    vector = _get(folder, "MediaVec")
    templates = list(vector)
    locks = _get(project, "LockableBlobMap/Sm2LockableBlobMap/LocableBlobSet")
    metadata_locks = [element for element in locks if element.find("BtMetadataLockableBlob") is not None]
    for element in metadata_locks:
        locks.remove(element)
    vector.clear()
    globals_ = {folder.attrib["DbId"], master.attrib["DbId"], _get(folder, "MediaPool").text or ""}
    for index, item in enumerate(media):
        pair = [copy.deepcopy(template) for template in templates]
        lock_pair = [copy.deepcopy(element) for element in metadata_locks]
        _fresh([*pair, *lock_pair], globals_)
        for element in pair:
            clip = element[0]
            audio = clip.tag == "Sm2MpAudioClip"
            if audio and item.channels == 0:
                continue
            _edit(clip, item, audio=audio)
            if audio:
                _annotate_audio(clip, item)
            vector.append(element)
            owner = _get(clip, "EmbeddedAudioVec/Element/BtAudioInfo" if audio else "Video/BtVideoInfo")
            if not audio and rotations is not None:
                node, data, mode = _payload(owner, "Radiometry")
                node.text = wrap(clip_rotation(data, rotations[index]), mode).hex()
            proxy_item = proxy_items[index]
            if not audio and proxy_item is not None:
                attach_proxy(clip, proxy_item)
            for lock in lock_pair:
                if lock.findtext("BtMetadataLockableBlob/BlobOwner") == owner.attrib["DbId"]:
                    if not audio:
                        _annotate(clip, lock, item)
                    locks.append(lock)
        if audio_linked(item) and (not separate_audio or link_audio):
            video = next(element[0] for element in pair if element[0].tag == "Sm2MpVideoClip")
            audio_clip = next(element[0] for element in pair if element[0].tag == "Sm2MpAudioClip")
            _sync_audio(video, audio_clip, item)
            if not separate_audio:
                _embed_audio(video, audio_clip)
                vector.remove(next(element for element in pair if element[0] is audio_clip))
    # The folder path is the one Resolve accepted from the synthetic export, before the rename from
    # mcstudio; the bin's name is the folder's Name above.
    entries = ("project.xml", "MediaPool/Master/MpFolder.xml", "MediaPool/Master/000_mcstudio/MpFolder.xml")
    # Recheck all snapshots after assembly. A launch failure can then retain this completed file.
    if any(source_stamp(mount.source) != mount.stamp for mount in mounts):
        raise ValueError("Source changed while creating the archive")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with ZipFile(temporary, "x", compression=ZIP_DEFLATED) as archive:
            for filename, tree in zip(entries, trees, strict=True):
                text = ET.tostring(tree, encoding="unicode").replace("ListMgt__", "ListMgt::")
                archive.writestr(filename, '<?xml version="1.0" encoding="UTF-8"?>\n' + _STAMP + text)
        # The service allocates a fresh per-send folder; never replace an existing archive.
        path.hardlink_to(temporary)
    finally:
        temporary.unlink(missing_ok=True)
