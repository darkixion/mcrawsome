"""Native Resolve 21.1 timelines with paired CinemaDNG video and aligned WAV audio.

The sanitised scaffold and reciprocal partner links were accepted in Resolve with actual playback
and video/audio rendering. Media Pool RAW defaults use the shared measured bin descriptor writer.
"""

from __future__ import annotations

import copy
import math
import shutil
import struct
import threading
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from zipfile import ZIP_DEFLATED, ZipFile

from mcrawsome.mount.limits import MAX_MOUNTS
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import source_stamp
from mcrawsome.resolve.bin_archive import (
    Media,
    _annotate,
    _annotate_audio,
    _edit,
    _embed_audio,
    _fresh,
    _get,
    _map,
    _payload,
    _sync_audio,
    attach_proxy,
    audio_linked,
    mounted_media,
    proxy_media,
)
from mcrawsome.resolve.bin_codec import (
    MapField,
    WireField,
    decode_map,
    decode_wire,
    encode_map,
    encode_wire,
    patch_map,
    patch_wire,
    wrap,
)
from mcrawsome.resolve.camera_gyro import CameraGyroCorrections
from mcrawsome.resolve.camera_gyro import version as camera_gyro_version
from mcrawsome.resolve.geometry import matching_aspect
from mcrawsome.resolve.markers import Marker
from mcrawsome.resolve.markers import attach as attach_markers
from mcrawsome.resolve.rotation import clip_rotation
from mcrawsome.resolve.stabilisation import PerspectiveCorrections, version

_SEED = Path(__file__).parent / "data/timeline_21_1"
PREPARED_AT_ONCE = 2  # stabilised clips analysed side by side, each decoding with its own worker processes


def read(path: Path) -> ET.Element:
    return ET.fromstring(path.read_text().replace("ListMgt::", "ListMgt__"))


def section(raw: bytes, number: int) -> bytes:
    value = next(f.value for f in decode_wire(raw) if f.number == number)
    if not isinstance(value, bytes):
        raise ValueError("Unexpected timeline scaffold")
    return value


def media_start(timecode: str, rate: float) -> str:
    """MediaStartTime: where a clip begins on its own clock, in seconds, as Resolve's own timelines write it
    (41436.18645833333 for a 23.976 fps clip whose Start TC is 11:29:54:19); "0" for a clip counting from
    zero. The timecode counts frames at its whole rate, played at the clip's."""
    if not timecode:
        return "0"
    hours, minutes, seconds, frames = (int(part) for part in timecode.split(":"))
    return f"{(((hours * 60 + minutes) * 60 + seconds) * round(rate) + frames) / rate:.16g}"


def timing(node: ET.Element, media: Media, start: int, pool: ET.Element, partner: str | None) -> None:
    audio = node.tag == "Sm2TiAudioClip"
    combined = pool.tag == "Sm2MpVideoClip"
    filename = media.stem + ".wav" if audio and not combined else media.video_name
    for key, value in {
        "Name": filename,
        "Start": str(start),
        "Duration": str(media.frames),
        "MediaRef": pool.attrib["DbId"],
        # Resolve reads the item on its clip's clock, which begins at the Start TC in the video clip's Time
        # record; left at 0 it reads frames from before the clip. A separate WAV clip counts from zero.
        "MediaStartTime": media_start(media.timecode, media.rate) if combined else "0",
        "MediaFilePath": str(media.folder / filename),
        "MediaFrameRate": struct.pack("<dd", media.rate, 0).hex(),
        "MediaTimemapBA": (b"\x02" + struct.pack(">d", (media.frames - 1) / media.rate)).hex(),
    }.items():
        _get(node, key).text = value
    blob, raw, mode = _payload(node, "FieldsBlob")
    outer = section(raw, 1)
    inner = encode_wire([field for field in decode_wire(section(outer, 1)) if field.number != 1])
    inner = patch_wire(inner, {21: start})
    if partner:
        link = encode_map({"0": MapField(10, 0, partner.encode("utf-16-be"))})
        inner = encode_wire([WireField(1, 2, link), *decode_wire(inner)])
    blob.text = wrap(patch_wire(raw, {1: patch_wire(outer, {1: inner})}), mode).hex()
    if audio:
        _map(
            node,
            "VirtualAudioTrackBA",
            {
                "AudioType": struct.pack(">i", 1 if media.channels == 1 else 0),
                "ChannelsBA": struct.pack(">II", 2, media.channels)
                + b"".join(
                    struct.pack(">I", (0x20004001 if combined else 0x4001) + channel)
                    for channel in range(media.channels)
                ),
            },
        )


def timeline_size(media: list[Media], rotations: list[int] | None = None) -> tuple[int, int]:
    """Match displayed source dimensions exactly, including portrait and above-UHD media."""
    width, height = media[0].info.width, media[0].info.height
    if rotations and rotations[0] % 180:
        width, height = height, width
    return width, height


def fitted_timeline_size(size: tuple[int, int], full_resolution: bool) -> tuple[int, int]:
    """Use the largest fitting dimensions, rounded to the nearest even output pixel."""
    width, height = size
    if full_resolution or (width <= 3840 and height <= 2160):
        return size
    scale = min(3840 / width, 2160 / height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def configure(
    sequence: ET.Element,
    pool: ET.Element,
    media: list[Media],
    rotations: list[int] | None = None,
    *,
    full_resolution: bool = True,
) -> None:
    rate = media[0].rate
    width, height = fitted_timeline_size(timeline_size(media, rotations), full_resolution)
    portrait = height > width
    extent = struct.pack("<dd", 3600, sum(m.frames for m in media) / rate)
    _get(sequence, "MediaExtents").text = extent.hex()
    _get(sequence, "FrameRate").text = struct.pack("<dd", rate, 0).hex()
    blob, raw, mode = _payload(sequence, "FieldsBlob")
    fields = decode_map(raw)
    fields.pop("Thumbnail", None)
    setup = bytearray(fields["SequenceSetup"].value or b"")
    if len(setup) != 365:
        raise ValueError("Unexpected sequence setup")
    for offset, value in ((44, width), (52, height), (80, int(rate)), (232, width), (240, height)):
        struct.pack_into(">I", setup, offset, value)
    if portrait:
        # Native custom timeline settings from the portrait reference. Without
        # these flags Resolve inherits the project's landscape format.
        struct.pack_into(">Q", setup, 100, 1)
        struct.pack_into(">Q", setup, 108, 1)
        fields_tail = {f.number: f for f in decode_wire(bytes(setup[344:]))}
        fields_tail[17] = WireField(17, 0, 68)
        fields_tail[44] = WireField(44, 0, 14)
        tail = encode_wire([fields_tail[n] for n in sorted(fields_tail)])
        setup = setup[:340] + struct.pack(">I", len(tail)) + tail
        _get(sequence, "Resolution").text = struct.pack(">QQ", width, height).hex()
    fields["SequenceSetup"] = MapField(12, 0, bytes(setup))
    fields["LastModTimeForCache"] = MapField(4, 0, b"\0" * 8)
    blob.text = wrap(encode_map(fields), mode).hex()
    # Native sequence geometry is nested Body/field1/field3, not top-level fields.
    body, raw, mode = _payload(sequence, ".//Body")
    one = section(raw, 1)
    geometry: dict[int, int | bytes] = {1: width, 2: height, 4: width, 5: height, 7: width, 8: height}
    if portrait:
        geometry[9] = 1
    three = patch_wire(section(one, 3), geometry)
    body.text = wrap(patch_wire(raw, {1: patch_wire(one, {3: three})}), mode).hex()
    blob, raw, mode = _payload(pool, "FieldsBlob")
    one = section(raw, 1)
    inner = section(one, 1)
    tracks = section(inner, 5)
    track = decode_map(tracks)["0"].value or b""
    track = patch_map(track, {"MediaExtents": extent})
    blob.text = wrap(
        patch_wire(raw, {1: patch_wire(one, {1: patch_wire(inner, {5: patch_map(tracks, {"0": track})})})}),
        mode,
    ).hex()


def _fps(rate: float) -> str:
    return f"{rate:.3f}".rstrip("0").rstrip(".")


def _listed(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


class Rated(Protocol):
    @property
    def stem(self) -> str: ...
    @property
    def rate(self) -> float: ...


def usable_rate(rate: float) -> bool:
    """Whether a Resolve timeline can take a clip at this rate: a whole number of frames from 1 to 60."""
    return math.isfinite(rate) and rate == int(rate) and 1 <= rate <= 60


def unusable_rates(media: Sequence[Rated]) -> str | None:
    """Why a timeline cannot take some of these clips, naming every one with its rate and the setting that
    fixes it, not only the first; None when it can take them all."""
    odd = [item for item in media if not usable_rate(item.rate)]
    if len(odd) == 1:
        return (
            f"{odd[0].stem} records {_fps(odd[0].rate)} fps, and Resolve timelines need a whole number of "
            "frames per second from 1 to 60. In its Resolve Settings, under RAW media, set Mounted frame "
            "rate to Nearest whole rate (or a fixed rate) and send it again; a Media Bin send keeps the "
            "recorded rate."
        )
    if odd:
        named = _listed([f"{item.stem} ({_fps(item.rate)} fps)" for item in odd])
        return (
            f"{len(odd)} clips record rates Resolve timelines cannot use, which need a whole number of "
            f"frames per second from 1 to 60: {named}. Select them, set RAW media > Mounted frame rate to "
            "Nearest whole rate in Resolve Settings and use Apply to Selected (or Set as Default for every "
            "clip), then send again; a Media Bin send keeps the recorded rates."
        )
    return None


def check_rates(media: Sequence[Media]) -> None:
    """Resolve timelines take one whole frame rate from 1 to 60. A send puts each rate on a timeline of
    its own; this guards a timeline written with several."""
    if (problem := unusable_rates(media)) is not None:
        raise ValueError(problem)
    rates = sorted({item.rate for item in media})
    if len(rates) > 1:
        common = max(rates, key=lambda rate: sum(item.rate == rate for item in media))
        group = [item for item in media if item.rate == common]
        parts = [
            f"{group[0].stem} is {_fps(common)} fps"
            if len(group) == 1
            else f"{len(group)} clips are {_fps(common)} fps"
        ]
        parts += [f"{item.stem} is {_fps(item.rate)} fps" for item in media if item.rate != common]
        raise ValueError(
            f"Resolve timelines need every clip at one frame rate: {_listed(parts)}. Send them as "
            "separate timelines, or set RAW media > Mounted frame rate to the same fixed rate for them in "
            "Resolve Settings."
        )


def _write(
    path: Path,
    media: list[Media],
    *,
    name: str,
    linked: bool = True,
    separate_audio: bool = True,
    check: Callable[[], None] | None = None,
    corrections: Sequence[PerspectiveCorrections | CameraGyroCorrections | None] | None = None,
    rotations: list[int] | None = None,
    markers: list[list[Marker]] | None = None,
    full_resolution: bool = True,
    proxies: list[Media | None] | None = None,
) -> None:
    if not media or len(media) > MAX_MOUNTS or not name.strip():
        raise ValueError(f"Select between 1 and {MAX_MOUNTS} clips")
    if proxies is not None and len(proxies) != len(media):
        raise ValueError("Expected one optional proxy per original clip")
    rate = media[0].rate
    if rotations is not None and (
        len(rotations) != len(media) or any(r not in (0, 90, 180, 270) for r in rotations)
    ):
        raise ValueError("Expected one clockwise quarter-turn display rotation per timeline clip")
    check_rates(media)
    for item in media:
        if item.frames < 1:
            raise ValueError(f"{item.stem} has no frames to place on a timeline")
        if item.channels not in (0, 1, 2):
            raise ValueError(
                f"{item.stem} has {item.channels} audio channels; timelines take mono or stereo audio"
            )
        if item.channels and (item.samples < 1 or item.sample_rate < 1):
            raise ValueError("Invalid audio metadata")
        if not 1 <= item.info.width <= 65535 or not 1 <= item.info.height <= 65535:
            raise ValueError("Invalid geometry")

    source_size = timeline_size(media, rotations)
    output_size = fitted_timeline_size(source_size, full_resolution)
    if corrections is not None:
        if len(corrections) != len(media):
            raise ValueError("Expected a gyro plan for every timeline clip")
        target_width, target_height = output_size
        for index, (item, correction) in enumerate(zip(media, corrections, strict=True)):
            if correction is None:  # this clip is not stabilised
                continue
            correction.validate(item.frames, item.info.width, item.info.height, item.info.orientation)
            w, h = item.info.width, item.info.height
            if rotations and rotations[index] % 180:
                w, h = h, w
            if not matching_aspect((w, h), (target_width, target_height)):
                raise ValueError("Gyro timelines require matching displayed aspect ratios")

    if any(
        timeline_size([item], [rotations[index]] if rotations else None) != source_size
        for index, item in enumerate(media)
    ):
        raise ValueError(
            "Each timeline needs matching displayed resolutions; "
            "MCRAWsome splits resolution groups when sending."
        )

    project, master, container = [read(_SEED / n) for n in ("project.xml", "master.xml", "container.xml")]
    pool_timeline = _get(master, "MediaVec/Element/Sm2MpTimelineClip")
    templates = [copy.deepcopy(n) for n in _get(master, "MediaVec") if n[0].tag != "Sm2MpTimelineClip"]
    timeline = _get(pool_timeline, "TimelineSharedHandle/Sm2Timeline")
    sequence = _get(timeline, "Sequence/Sm2Sequence")
    _get(project, "ProjectName").text = name
    # Timeline projects have one registered handle and a current timeline index.
    handles = _get(project, "TimelineHandleVec")
    handles.clear()
    ET.SubElement(handles, "Element").text = timeline.attrib["DbId"]
    _get(project, "CurrentTimelineIndex").text = "0"
    for key in ("Name", "ClipName"):
        node = pool_timeline.find(key)
        if node is not None:
            node.text = name
    _get(timeline, "Name").text = name
    for key in ("ModTimeInSecs", "CreateTimeInSecs"):
        _get(timeline, key).text = "0"
    _get(pool_timeline, "MpFolder").text = master.attrib["DbId"]
    _get(master, "FieldsBlob").text = None  # Remove the DRB child-folder list.
    vector = _get(master, "MediaVec")
    vector.clear()
    ET.SubElement(vector, "Element").append(pool_timeline)
    configure(sequence, pool_timeline, media, rotations, full_resolution=full_resolution)

    locks = _get(project, "LockableBlobMap/Sm2LockableBlobMap/LocableBlobSet")
    if markers is not None:
        attach_markers(locks, sequence.attrib["DbId"], markers, [m.frames for m in media])
    lock_templates = [copy.deepcopy(n) for n in locks if n.find("BtMetadataLockableBlob") is not None]
    for n in list(locks):
        if n.find("BtMetadataLockableBlob") is not None:
            locks.remove(n)
    video_items = _get(container, "VideoTrackVec/Element/Sm2TiTrack/Items")
    audio_items = _get(container, "AudioTrackVec/Element/Sm2TiTrack/Items")
    video_template, audio_template = copy.deepcopy(video_items[0]), copy.deepcopy(audio_items[0])
    video_items.clear()
    audio_items.clear()
    shared = {master.attrib["DbId"], _get(master, "MediaPool").text or ""}
    editorial: list[tuple[ET.Element, ET.Element, ET.Element, Media]] = []
    audio_names: list[tuple[ET.Element, ET.Element, Media]] = []
    start = round(3600 * rate)
    for index, item in enumerate(media):
        if check is not None:
            check()
        pool_pair = copy.deepcopy(templates)
        pair_locks = copy.deepcopy(lock_templates)
        for element in pool_pair:
            _get(element[0], "MpFolder").text = master.attrib["DbId"]
        _fresh([*pool_pair, *pair_locks], shared)
        video = next(n[0] for n in pool_pair if n[0].tag == "Sm2MpVideoClip")
        audio = next(n[0] for n in pool_pair if n[0].tag == "Sm2MpAudioClip")
        linked_item = (not separate_audio or linked) and audio_linked(item)
        ti_pair = [copy.deepcopy(video_template), copy.deepcopy(audio_template)]
        _fresh(ti_pair)
        for pool, ti, partner in ((video, ti_pair[0], ti_pair[1]), (audio, ti_pair[1], ti_pair[0])):
            is_audio = pool.tag == "Sm2MpAudioClip"
            if is_audio and not item.channels:
                continue
            _edit(pool, item, audio=is_audio)
            if is_audio:
                audio_names.append((pool, ti[0], item))
            element = next(n for n in pool_pair if n[0] is pool)
            vector.append(element)
            owner = _get(pool, "EmbeddedAudioVec/Element/BtAudioInfo" if is_audio else "Video/BtVideoInfo")
            for lock in pair_locks:
                if lock.findtext("BtMetadataLockableBlob/BlobOwner") == owner.attrib["DbId"]:
                    locks.append(lock)
                    if not is_audio:
                        editorial.append((pool, ti[0], lock, item))
            timing(
                ti[0],
                item,
                start,
                video if is_audio and not separate_audio else pool,
                partner[0].attrib["DbId"] if linked_item else None,
            )
            if not is_audio and rotations is not None:
                node, raw, mode = _payload(owner, "Radiometry")
                node.text = wrap(clip_rotation(raw, rotations[index]), mode).hex()
            proxy_item = proxies[index] if proxies is not None else None
            if not is_audio and proxy_item is not None:
                attach_proxy(pool, proxy_item)
            if not is_audio and corrections is not None and corrections[index] is not None:
                correction = corrections[index]
                assert correction is not None
                turn = rotations[index] if rotations else 0
                if isinstance(correction, CameraGyroCorrections):
                    local_version = camera_gyro_version(
                        correction, output_size, display_rotation=turn, check=check or (lambda: None)
                    )
                else:
                    local_version = version(
                        correction, output_size, display_rotation=turn, check=check or (lambda: None)
                    )
                ti[0].append(local_version)
                # Version tables and their active-version pointers must be unique per clip.
                _fresh([_get(ti[0], "pLmVerTable")])
            (audio_items if is_audio else video_items).append(ti)
        if linked_item:
            _sync_audio(video, audio, item)
            if not separate_audio:
                _embed_audio(video, audio)
                vector.remove(next(element for element in pool_pair if element[0] is audio))
        start += item.frames
    for track in container.iter("Sm2TiTrack"):
        _get(track, "Sequence").text = sequence.attrib["DbId"]
    trees = [project, master, container]
    _fresh(trees)
    for pool, ti_video, lock, item in editorial:
        _annotate(pool, lock, item)
        _get(ti_video, "Name").text = item.display_name
    for pool, ti_audio, item in audio_names:
        _annotate_audio(pool, item)
        _get(ti_audio, "Name").text = item.audio_display_name
    entries = ["project.xml", "MediaPool/Master/MpFolder.xml", f"SeqContainer/{container.attrib['DbId']}.xml"]
    if check is not None:
        check()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with ZipFile(temporary, "x", compression=ZIP_DEFLATED) as archive:
            for filename, tree in zip(entries, trees, strict=True):
                text = ET.tostring(tree, encoding="unicode").replace("ListMgt__", "ListMgt::")
                archive.writestr(
                    filename,
                    '<?xml version="1.0" encoding="UTF-8"?>\n<!--DbAppVer="21.1.1.0010" DbPrjVer="17"-->\n'
                    + text,
                )
        if check is not None:
            check()
        path.hardlink_to(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def write_timeline(
    path: Path,
    mounts: list[MountedSequence],
    *,
    name: str,
    stabilise: bool = False,
    full_resolution: bool = True,
    separate_audio: bool = False,
    link_audio: bool = True,
    rotations: list[int] | None = None,
    proxies: list[MountedSequence | None] | None = None,
    check: Callable[[], None] = lambda: None,
    progress: Callable[[MountedSequence | None, str], None] | None = None,
) -> int:
    """Write one timeline atomically and return how many of its clips are stabilised. Gyro analysis
    never changes DNG pixels. `progress` is told each clip's stage, then the timeline's (clip None).

    With `stabilise`, each clip whose saved settings ask for it (`gyro.wants_stabilisation`) gets Camera
    Gyro corrections from its own crop, strength, smoothing and rolling-shutter setting; the others are
    left as recorded. A stabilised timeline keeps regeneration provenance in <archive>.gyro. The companion
    directory is complete before publishing the DRT and removed if publication fails.
    `check` may raise to cancel; it runs between analysis stages and cache frames.
    `rotations` are extra clockwise display turns, applied through Clip Attributes;
    callers must supply media mounts with RawCalibration.rotation zero in this mode.
    """
    if not 1 <= len(mounts) <= MAX_MOUNTS or not name.strip():
        raise ValueError(f"Select between 1 and {MAX_MOUNTS} mounted clips")
    if rotations is not None and (
        len(rotations) != len(mounts)
        or any(r not in (0, 90, 180, 270) for r in rotations)
        or any(m.raw_settings.rotation for m in mounts)
    ):
        raise ValueError("Timeline display rotation requires media mounted in its recorded orientation")
    check()
    if path.exists():
        raise FileExistsError(path)
    media = [mounted_media(mount) for mount in mounts]
    proxy_items = proxy_media(mounts, media, proxies)

    def sources_unchanged() -> None:
        check()
        if any(source_stamp(mount.source) != mount.stamp for mount in mounts):
            raise ValueError("Source changed while creating the timeline archive")

    from mcrawsome.resolve.gyro import PreparedGyro, clip_recipe, prepare, wants_stabilisation
    from mcrawsome.resolve.motion_markers import prepare as prepare_markers

    wanted = [stabilise and wants_stabilisation(mount.source) for mount in mounts]

    def told(mount: MountedSequence | None, text: str) -> None:
        if progress is not None:
            progress(mount, text)

    marked = [
        clip_recipe(mount.source).motion.markers
        and any(clip_recipe(mount.source).motion.review_categories.model_dump().values())
        for mount in mounts
    ]
    if not any(wanted):
        marker_groups, checks = [], []
        for index, (mount, item, enabled) in enumerate(zip(mounts, media, marked, strict=True)):
            if enabled:
                told(mount, "Finding review markers")
            group, sensor_check = (
                prepare_markers(
                    mount,
                    item,
                    check=sources_unchanged,
                    display_rotation=rotations[index] if rotations else 0,
                )
                if enabled
                else ([], sources_unchanged)
            )
            marker_groups.append(group)
            checks.append(sensor_check)

        def unchanged_markers() -> None:
            sources_unchanged()
            for sensor_check in checks:
                sensor_check()

        told(None, "Writing the timeline")
        _write(
            path,
            media,
            name=name,
            check=unchanged_markers,
            rotations=rotations,
            markers=marker_groups if any(marked) else None,
            full_resolution=full_resolution,
            separate_audio=separate_audio,
            linked=link_audio,
            proxies=proxy_items,
        )
        return 0

    companion = path.with_name(path.name + ".gyro")
    if companion.exists():
        raise FileExistsError(companion)
    published = False
    with TemporaryDirectory(prefix=".mcrawsome-gyro-", dir=path.parent) as staging:
        root = Path(staging)
        failed = threading.Event()

        def analysed() -> None:
            if failed.is_set():  # another clip failed: this send is over
                raise CancelledError
            sources_unchanged()

        def prepared_clip(index: int) -> PreparedGyro:
            try:
                return prepare(
                    mounts[index],
                    media[index],
                    root / str(index),
                    check=analysed,
                    display_rotation=rotations[index] if rotations is not None else 0,
                    stage=lambda text: told(mounts[index], text),
                )
            except BaseException:
                failed.set()
                raise

        with ThreadPoolExecutor(PREPARED_AT_ONCE, thread_name_prefix="gyro-plan") as pool:
            futures = {index: pool.submit(prepared_clip, index) for index, want in enumerate(wanted) if want}
            clips = {future: index for index, future in futures.items()}
            for future in as_completed(futures.values()):
                error = future.exception()
                if error is not None and not isinstance(error, CancelledError):
                    raise error  # the clip that failed, not one it stopped
                if error is None:
                    told(mounts[clips[future]], "Stabilised")
            prepared = [futures[index].result() if index in futures else None for index in range(len(mounts))]
        marker_groups, checks = [], []
        for index, (mount, item, enabled, state) in enumerate(
            zip(mounts, media, marked, prepared, strict=True)
        ):
            if enabled:
                told(mount, "Finding review markers")
            group, sensor_check = (
                prepare_markers(
                    mount,
                    item,
                    state.motion if state is not None else None,
                    check=sources_unchanged,
                    plan=state.plan if state is not None else None,
                    display_rotation=rotations[index] if rotations else 0,
                )
                if enabled
                else ([], sources_unchanged)
            )
            marker_groups.append(group)
            checks.append(sensor_check)

        def unchanged() -> None:
            sources_unchanged()
            for state in prepared:
                if state is not None:
                    state.check()
            for sensor_check in checks:
                sensor_check()

        unchanged()
        try:
            # mkdir is exclusive: do not replace existing provenance or somebody else's export.
            companion.mkdir()
            published = True
            for entry in root.iterdir():
                shutil.move(str(entry), companion / entry.name)
            told(None, "Writing the timeline")
            _write(
                path,
                media,
                name=name,
                check=unchanged,
                corrections=[p.corrections if p is not None else None for p in prepared],
                rotations=rotations,
                markers=marker_groups if any(marked) else None,
                full_resolution=full_resolution,
                separate_audio=separate_audio,
                linked=link_audio,
                proxies=proxy_items,
            )
        except BaseException:
            if published:
                shutil.rmtree(companion)
            raise
    return sum(wanted)
