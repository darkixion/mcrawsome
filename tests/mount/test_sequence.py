import errno
import io
import wave
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import rawpy

from mcrawsome.io.clip import Clip
from mcrawsome.io.describe import RateChoice
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.audio import aligned_track, write_wav
from mcrawsome.output.dng import dng_info, read_dng_tags, uncompressed_header, write_dng


def test_directory_scans_cache_sizes_without_retaining_every_full_header(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_mcraw(tmp_path / "scan.mcraw", [i * 41666667 for i in range(40)])
    reads = []
    original = Clip.frame_metadata

    def metadata(clip: Clip, index: int):  # type: ignore[no-untyped-def]
        reads.append(index)
        return original(clip, index)

    with Sequence(source) as sequence:
        monkeypatch.setattr(Clip, "frame_metadata", metadata)
        names = sequence.names()[:-1]
        expected = [sequence.size(name) for name in names]
        first_scan = len(reads)
        assert [sequence.size(name) for name in reversed(names)] == list(reversed(expected))
        assert len(reads) == first_scan, "a second stat pass rereads frame JSON"
        for name, size in zip(names, expected, strict=True):
            assert sequence.read(name, size, 1) == b""
        assert len(sequence.headers) <= 16


@pytest.mark.parametrize("gain_bytes", [0, 131000, 262144])
@pytest.mark.parametrize("noise", [None, (0.1, 0.2), (0.1, 0.2) * 3])
def test_cached_sizes_match_headers_across_alignment_boundaries(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch, gain_bytes: int, noise: tuple[float, ...] | None
) -> None:
    with Sequence(tiny_raw) as sequence:
        base = sequence.frame_info(0)
        infos = [
            replace(base, gain_maps=bytes(gain_bytes + index * 131072), noise_profile=noise)
            for index in range(3)
        ]
        monkeypatch.setattr(sequence, "frame_info", lambda index, factor=None: infos[index])
        for index, name in enumerate(sequence.names()[:-1]):
            header = uncompressed_header(infos[index])
            size = len(header) + base.width * base.height * 2
            assert sequence.size(name) == size
            assert sequence.read(name, 0, len(header)) == header
            assert sequence.size(name) == size


def test_metadata_and_header_reads_do_not_decode(tiny_raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> None:
        raise AssertionError("decoded a frame for metadata")

    monkeypatch.setattr(Clip, "raw_frame", forbidden)
    with Sequence(tiny_raw) as sequence:
        names = sequence.names()
        assert names == [f"tiny-{i:06d}.dng" for i in range(3)] + ["tiny.wav"]
        assert sequence.size(names[-1]) > 44
        for name in names[:-1]:
            assert sequence.size(name) > 128 * 64 * 2
            assert sequence.read(name, 0, 8) == b"II*\0\x08\0\0\0"
        assert sequence.read(names[0], sequence.size(names[0]), 10) == b""


def test_random_reads_match_export_and_keep_sensor_values(tiny_raw: Path, tmp_path: Path) -> None:
    with Sequence(tiny_raw) as sequence, Clip(tiny_raw) as clip:
        for index in (2, 0, 1):
            raw = clip.raw_frame(index)
            info = dng_info(clip.metadata, raw.metadata, sequence.rate, 0.0)
            # A mounted sequence fixes its white balance to the first frame's recorded neutral.
            path = tmp_path / "expected.dng"
            write_dng(path, raw.data, info)
            wanted = path.read_bytes()
            name = sequence.names()[index]
            got = b"".join(sequence.read(name, at, 997) for at in range(0, sequence.size(name), 997))
            physical_tags = read_dng_tags(path)
            path.write_bytes(got)
            virtual_tags = read_dng_tags(path)
            assert virtual_tags.pop(273)[0] % 65536 == 0
            physical_tags.pop(273)
            assert virtual_tags == physical_tags
            assert got[-raw.data.nbytes :] == wanted[-raw.data.nbytes :]
            with rawpy.imread(str(path)) as image:
                np.testing.assert_array_equal(image.raw_image, raw.data)


def test_cache_is_bounded_and_seeks_only_decode_requested_frames(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoded: list[int] = []
    original = Clip.raw_frame

    def decode(clip: Clip, index: int):  # type: ignore[no-untyped-def]
        decoded.append(index)
        return original(clip, index)

    monkeypatch.setattr(Clip, "raw_frame", decode)
    with Sequence(tiny_raw, cache_bytes=128 * 64 * 2) as sequence:
        for index in (2, 2, 0, 1, 2):
            name = sequence.names()[index]
            sequence.read(name, sequence.size(name) - 2, 2)
            assert sequence.cached_bytes <= 128 * 64 * 2
        assert decoded == [2, 0, 1, 2]


def test_pixel_views_survive_cache_eviction_and_close(tiny_raw: Path) -> None:
    with Sequence(tiny_raw, cache_bytes=128 * 64 * 2) as sequence:
        name = sequence.names()[0]
        offset = sequence.size(name) - 128 * 64 * 2
        pixels = sequence.read(name, offset, 1000)
        assert isinstance(pixels, memoryview)
        assert pixels.readonly
        assert pixels.c_contiguous
        expected = bytes(pixels)
        # The boundary response still combines header and pixels correctly.
        boundary = sequence.read(name, offset - 8, 16)
        assert boundary == b"\0" * 8 + expected[:8]
        other = sequence.names()[1]
        sequence.read(other, sequence.size(other) - 2, 2)
        assert (0, sequence.factor) not in sequence.pixels
        assert bytes(pixels) == expected
    assert bytes(pixels) == expected
    with pytest.raises(TypeError):
        pixels[0] = 0


def test_stat_then_read_reuses_bounded_frame_info(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_mcraw(tmp_path / "metadata.mcraw", [i * 41666667 for i in range(40)])
    with Sequence(source) as sequence:
        calls: list[int] = []
        original = sequence.clip.frame_metadata

        def metadata(index: int):  # type: ignore[no-untyped-def]
            calls.append(index)
            return original(index)

        monkeypatch.setattr(sequence.clip, "frame_metadata", metadata)
        for index, name in enumerate(sequence.names()[:-1]):
            sequence.size(name)
            sequence.read(name, 0, 8)
            assert calls.count(index) == 1
            assert len(sequence.infos) <= 16


def test_audio_random_reads_match_aligned_export(tiny_raw: Path, tmp_path: Path) -> None:
    with Sequence(tiny_raw) as sequence, Clip(tiny_raw) as clip:
        track = clip.audio()
        assert track is not None
        expected = tmp_path / "audio.wav"
        write_wav(expected, aligned_track(track, int(clip.timestamps_ns[0]), 3 / 24, sequence.tempo))
        data = b"".join(sequence.read("tiny.wav", at, 157) for at in range(0, sequence.size("tiny.wav"), 157))
        assert data == expected.read_bytes()
        with wave.open(io.BytesIO(data)) as wav:
            assert wav.getnframes() == 6000


def test_source_changes_fail_instead_of_changing_cached_media(tiny_raw: Path) -> None:
    with Sequence(tiny_raw) as sequence:
        name = sequence.names()[0]
        sequence.read(name, sequence.size(name) - 2, 2)
        tiny_raw.rename(tiny_raw.with_suffix(".offline"))
        with pytest.raises(OSError, match="changed or is offline"):
            sequence.size(name)
        with pytest.raises(OSError, match="changed or is offline") as missing:
            sequence.read(name, 0, 8)
        assert missing.value.errno == errno.EIO


@pytest.mark.parametrize("name", ["../tiny.mcraw", "tiny-999999.dng", "tiny_0.dng", "/tiny-000000.dng"])
def test_unknown_names_cannot_escape_sequence(tiny_raw: Path, name: str) -> None:
    with Sequence(tiny_raw) as sequence, pytest.raises(FileNotFoundError):
        sequence.size(name)


@pytest.mark.parametrize("step", [41_666_667, 41_663_194])
@pytest.mark.parametrize("offset_ms", [-20, 0, 20])
@pytest.mark.parametrize("stamps_ms", [[0, 30, 60], [0, 120, 240], [0, 120, 0], [0, None, 60]])
def test_streamed_audio_matches_export_with_gaps_overlaps_and_drift(
    tiny_raw: Path,
    tmp_path: Path,
    step: int,
    offset_ms: int,
    stamps_ms: list[int | None],
) -> None:
    from mcrawsome.io.container import ContainerWriter, read_frame_record, read_index

    path = tmp_path / "timed.mcraw"
    with tiny_raw.open("rb") as source:
        index = read_index(source)
        with ContainerWriter(path, index.metadata) as writer:
            for i, ref in enumerate(index.frames):
                payload, metadata = read_frame_record(source, ref.offset)
                writer.add_frame(1_000_000_000 + i * step, payload, metadata)
            for i, stamp in enumerate(stamps_ms):
                samples = (np.arange(2000, dtype=np.int16).reshape(-1, 2) * (i + 1) - 2000).astype("<i2")
                writer.add_audio(
                    samples.tobytes(),
                    None if stamp is None else 1_000_000_000 + (stamp + offset_ms) * 1_000_000,
                )
    with Sequence(path) as sequence, Clip(path) as clip:
        track = clip.audio()
        assert track is not None
        expected = tmp_path / "aligned.wav"
        write_wav(
            expected,
            aligned_track(
                track,
                int(clip.timestamps_ns[0]),
                len(sequence.sources) / float(sequence.rate),
                sequence.tempo,
            ),
        )
        name = "timed.wav"
        data = b"".join(sequence.read(name, at, 997) for at in range(0, sequence.size(name), 997))
        assert data == expected.read_bytes()


def test_a_baked_mount_carries_the_correction_in_its_values(tiny_raw: Path) -> None:
    from mcrawsome.output.dng import bake_shading, baked_white
    from mcrawsome.recipe.raw import RawCalibration

    baked = RawCalibration(mount_shading="baked")
    with (
        Sequence(tiny_raw, raw_settings=baked) as sequence,
        Sequence(tiny_raw) as plain,
        Clip(tiny_raw) as clip,
    ):
        for index in (1, 0, 2):
            raw = clip.raw_frame(index)
            info, normal = sequence.frame_info(index), plain.frame_info(index)
            assert normal.gain_maps
            assert info.gain_maps == b""
            assert info.white_level == baked_white(normal.black_levels, normal.white_level, 2)
            assert info.baseline_exposure == normal.baseline_exposure + 2
            name = sequence.names()[index]
            size = sequence.size(name)
            got = b"".join(sequence.read(name, at, 997) for at in range(0, size, 997))
            assert len(got) == size
            pixels = np.frombuffer(got[-raw.data.nbytes :], "<u2").reshape(raw.data.shape)
            expected, _ = bake_shading(
                raw.data, raw.metadata, clip.metadata.sensor_arrangement, 1.0, "full",
                normal.black_levels, normal.white_level, 2,
            )  # fmt: skip
            np.testing.assert_array_equal(pixels, expected)
            assert not np.array_equal(pixels, raw.data)


@pytest.mark.parametrize(("proxy", "factor"), [("half", 2), ("quarter", 4)])
def test_a_proxy_mount_serves_the_exports_reduced_dngs(
    tiny_raw: Path, tmp_path: Path, proxy: str, factor: int
) -> None:
    from mcrawsome.output.dng import reduce_cfa
    from mcrawsome.recipe.raw import RawCalibration

    with (
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_proxy=proxy)) as sequence,
        Clip(tiny_raw) as clip,
    ):
        raw = clip.raw_frame(1)
        info = dng_info(clip.metadata, raw.metadata, sequence.rate, 0.0)
        path = tmp_path / "expected.dng"
        write_dng(path, raw.data, info, scale=proxy)  # type: ignore[arg-type]
        name = sequence.names()[1]
        got = b"".join(sequence.read(name, at, 997) for at in range(0, sequence.size(name), 997))
        physical = read_dng_tags(path)
        path.write_bytes(got)
        virtual = read_dng_tags(path)
        physical.pop(273)
        virtual.pop(273)
        assert virtual == physical
        pixels = reduce_cfa(raw.data, clip.metadata.sensor_arrangement, factor)
        assert got[-pixels.nbytes :] == pixels.astype("<u2").tobytes()
        assert (sequence.frame_info(1).width, sequence.frame_info(1).height) == (128 // factor, 64 // factor)


def test_a_proxy_mount_can_serve_a_full_size_first_frame(tiny_raw: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    settings = RawCalibration(mount_proxy="half", mount_first_frame="full")
    with (
        Sequence(tiny_raw, raw_settings=settings) as sequence,
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_proxy="half")) as proxies,
        Sequence(tiny_raw) as whole,
    ):
        first, second = sequence.names()[:2]
        assert (sequence.first_info().width, sequence.first_info().height) == (128, 64)
        for name, expected in ((first, whole), (second, proxies)):
            size = sequence.size(name)
            assert size == expected.size(name)
            got = b"".join(sequence.read(name, at, 4093) for at in range(0, size, 4093))
            assert got == b"".join(expected.read(name, at, 4093) for at in range(0, size, 4093))
        # The first frame's size survives later reads of the proxies, and reading it again.
        assert sequence.size(first) == whole.size(first)
    with Sequence(tiny_raw, raw_settings=RawCalibration(mount_first_frame="full")) as unscaled:
        assert unscaled.size(unscaled.names()[0]) == unscaled.size(unscaled.names()[1])


def test_scaled_mounts_reach_16_bit_white_in_proportion(tiny_raw: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    with (
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_scale=True)) as scaled,
        Sequence(tiny_raw) as recorded,
    ):
        before, after = recorded.frame_info(1), scaled.frame_info(1)
        scale = 65535 / before.white_level
        assert after.white_level == 65535
        assert after.black_levels == pytest.approx([level * scale for level in before.black_levels])
        name = scaled.names()[1]
        count = before.width * before.height * 2
        values = np.frombuffer(recorded.read(name, recorded.size(name) - count, count), "<u2")
        got = np.frombuffer(scaled.read(name, scaled.size(name) - count, count), "<u2")
        expected = np.minimum(np.rint(values * np.float32(scale)), 65535)
        np.testing.assert_array_equal(got, expected)


def test_scaling_needs_linear_values(tiny_raw: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    with pytest.raises(ValueError, match="linear mount"):
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_scale=True, mount_transfer="log"))


def test_a_colour_matrix_override_reaches_the_mounted_dngs(tiny_raw: Path, tmp_path: Path) -> None:
    matrix = (0.9, -0.2, -0.1, -0.4, 1.3, 0.1, -0.05, 0.15, 0.6)
    with Sequence(tiny_raw, colour_matrix=matrix) as sequence:
        info = sequence.frame_info(0)
        assert info.colour_matrix_1 == pytest.approx(matrix)
        assert info.colour_matrix_2 == pytest.approx(matrix)
        name = sequence.names()[0]
        path = tmp_path / "frame.dng"
        path.write_bytes(
            b"".join(sequence.read(name, at, 1 << 20) for at in range(0, sequence.size(name), 1 << 20))
        )
    tags = read_dng_tags(path)
    assert [float(value) for value in tags[50721]] == pytest.approx(matrix, abs=1e-4)


def test_a_baked_proxy_reduces_the_baked_values(tiny_raw: Path) -> None:
    from mcrawsome.output.dng import bake_shading, reduce_cfa
    from mcrawsome.recipe.raw import RawCalibration

    settings = RawCalibration(mount_proxy="half", mount_shading="baked")
    with (
        Sequence(tiny_raw, raw_settings=settings) as sequence,
        Sequence(tiny_raw) as plain,
        Clip(tiny_raw) as clip,
    ):
        raw, normal = clip.raw_frame(0), plain.frame_info(0)
        baked, _ = bake_shading(
            raw.data, raw.metadata, clip.metadata.sensor_arrangement, 1.0, "full", normal.black_levels,
            normal.white_level, 2,
        )  # fmt: skip
        pixels = reduce_cfa(baked, clip.metadata.sensor_arrangement, 2)
        name = sequence.names()[0]
        got = b"".join(sequence.read(name, at, 4096) for at in range(0, sequence.size(name), 4096))
        assert got[-pixels.nbytes :] == pixels.astype("<u2").tobytes()
        assert sequence.frame_info(0).gain_maps == b""


@pytest.mark.parametrize(
    ("choice", "rate"), [("integer", 30), ("drop-frame", Fraction(30000, 1001)), (25.0, 25)]
)
def test_a_mount_converts_to_a_constant_frame_rate_keeping_the_sound_speed(
    make_mcraw: Callable[..., Path], tmp_path: Path, choice: str | float, rate: Fraction | int
) -> None:
    from mcrawsome.io.describe import rate_map
    from mcrawsome.recipe.raw import RawCalibration

    source = make_mcraw(tmp_path / "rate.mcraw", [i * 33_000_000 for i in range(12)])  # about 30.3 fps
    with (
        Sequence(source, raw_settings=RawCalibration(mount_frame_rate=choice)) as sequence,
        Clip(source) as clip,
    ):
        assert sequence.rate == rate
        assert sequence.sources == rate_map(clip.timestamps_ns, 1e9 / float(rate), 20e6)
        assert sequence.tempo == 1.0
        assert sequence.frame_info(0).frame_rate == rate
        assert len([n for n in sequence.names() if n.endswith(".dng")]) == len(sequence.sources)


@pytest.mark.parametrize("choice", ["keep", "integer", "drop-frame", "average", "median", 25.0])
@pytest.mark.parametrize("step_ns", [42_030_934, 33_000_000, 33_366_667])  # 23.79, 30.3 and 29.97 fps
def test_the_rate_a_clip_mounts_at_is_known_without_mounting_it(
    make_mcraw: Callable[..., Path], tmp_path: Path, choice: RateChoice, step_ns: int
) -> None:
    """A timeline send checks each clip's rate before mounting anything, so it must be the mount's own."""
    from mcrawsome.mount.sequence import mounted_rate
    from mcrawsome.recipe.raw import RawCalibration

    source = make_mcraw(tmp_path / "rate.mcraw", [i * step_ns for i in range(12)])
    with (
        Sequence(source, raw_settings=RawCalibration(mount_frame_rate=choice)) as sequence,
        Clip(source) as clip,
    ):
        assert mounted_rate(clip, choice) == sequence.rate


def test_an_auto_crop_mount_removes_a_black_edge_from_the_served_frames(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.develop.crop import CropBox, crop_raw
    from mcrawsome.recipe.raw import RawCalibration

    looked: list[int] = []

    def edges(frames: list[np.ndarray], black: float, white: float) -> CropBox:
        looked.append(len(frames))
        return CropBox(8, 0, 120, 64)

    monkeypatch.setattr("mcrawsome.mount.sequence.find_black_edges", edges)
    with (
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_crop="auto")) as sequence,
        Clip(tiny_raw) as clip,
    ):
        assert looked == [3]  # the first, middle and last frames, read once when mounting
        cropped = crop_raw(clip.raw_frame(2), CropBox(8, 0, 120, 64))
        info = dng_info(clip.metadata, cropped.metadata, sequence.rate, 0.0)
        path = tmp_path / "expected.dng"
        write_dng(path, cropped.data, info)
        name = sequence.names()[2]
        got = b"".join(sequence.read(name, at, 997) for at in range(0, sequence.size(name), 997))
        physical = read_dng_tags(path)
        path.write_bytes(got)
        virtual = read_dng_tags(path)
        physical.pop(273)
        virtual.pop(273)
        assert virtual == physical
        assert got[-cropped.data.nbytes :] == cropped.data.astype("<u2").tobytes()


def test_a_mount_without_black_edges_is_served_whole(tiny_raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    monkeypatch.setattr("mcrawsome.mount.sequence.find_black_edges", lambda *_: None)
    with Sequence(tiny_raw, raw_settings=RawCalibration(mount_crop="auto")) as sequence:
        assert (sequence.frame_info(0).width, sequence.frame_info(0).height) == (128, 64)


@pytest.mark.parametrize(
    ("override", "unique", "names"),
    [
        ("Panasonic Varicam RAW", False, ("Panasonic", "Varicam RAW", "Panasonic Varicam RAW")),
        (None, True, ("MotionCam", "{device} tiny", "MotionCam {device} tiny")),
    ],
)
def test_a_mount_can_name_another_camera_for_resolves_raw_tab(
    tiny_raw: Path, override: str | None, unique: bool, names: tuple[str, str, str]
) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    settings = RawCalibration(mount_camera_model=override, mount_unique_model=unique)
    with Sequence(tiny_raw, raw_settings=settings) as sequence, Clip(tiny_raw) as clip:
        device = clip.metadata.device_model or "MotionCam"
        info = sequence.frame_info(0)
        assert (info.make, info.model, info.unique_model) == tuple(n.format(device=device) for n in names)


def test_normalised_exposure_keeps_gain_maps_and_allows_a_stop_of_correction(tiny_raw: Path) -> None:
    from mcrawsome.output.dng import baked_white
    from mcrawsome.recipe.raw import RawCalibration

    settings = RawCalibration(mount_normalise_exposure=True)
    with (
        Sequence(tiny_raw, raw_settings=settings) as sequence,
        Sequence(tiny_raw) as plain,
        Clip(tiny_raw) as clip,
    ):
        info, normal = sequence.frame_info(1), plain.frame_info(1)
        assert info.gain_maps == normal.gain_maps  # the shading stays for the raw developer
        assert info.white_level == baked_white(normal.black_levels, normal.white_level, 1)
        assert info.baseline_exposure == normal.baseline_exposure + 1
        raw = clip.raw_frame(1)  # the synthetic frames share one exposure: the values are unchanged
        name = sequence.names()[1]
        got = b"".join(sequence.read(name, at, 4096) for at in range(0, sequence.size(name), 4096))
        assert got[-raw.data.nbytes :] == raw.data.astype("<u2").tobytes()


def test_a_marked_range_serves_only_its_frames_and_their_sound(tiny_raw: Path) -> None:
    with Sequence(tiny_raw) as whole, Sequence(tiny_raw, frame_range=(1, 2)) as marked:
        assert marked.sources == whole.sources[1:3]
        assert marked.names() == ["tiny-000000.dng", "tiny-000001.dng", "tiny.wav"]
        for index in range(2):
            name, source = f"tiny-{index:06d}.dng", f"tiny-{index + 1:06d}.dng"
            assert marked.read(name, 0, marked.size(name)) == whole.read(source, 0, whole.size(source))

        def pcm(sequence: Sequence) -> bytes:
            with wave.open(io.BytesIO(sequence.read("tiny.wav", 0, sequence.size("tiny.wav")))) as wav:
                return bytes(wav.readframes(wav.getnframes()))

        assert marked.wave is not None
        stride, begin = marked.wave.channels * 2, round(48000 / float(whole.rate))
        assert pcm(marked) == pcm(whole)[begin * stride : (begin + marked.wave.length) * stride]


@pytest.mark.parametrize("frame_range", [(3, 1), (-1, 2), (0, 0)])
def test_a_range_outside_the_recording_is_refused(tiny_raw: Path, frame_range: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="outside the recording"):
        Sequence(tiny_raw, frame_range=frame_range)


@pytest.mark.parametrize(("proxy", "factor"), [("full", 1), ("half", 2), ("quarter", 4), ("eighth", 8)])
def test_custom_crop_retains_top_left_pixels_sensor_geometry_and_audio(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch, proxy: str, factor: int
) -> None:
    from mcrawsome.io.clip import RawFrame
    from mcrawsome.output.dng import reduce_cfa
    from mcrawsome.recipe.raw import RawCalibration

    original = Clip.raw_frame

    def frame(clip: Clip, index: int) -> RawFrame:
        raw = original(clip, index)
        y, x = np.indices(raw.data.shape)
        data = np.asarray((y * 7 + x * 3 + index) % 900 + 64, dtype=np.uint16)
        return replace(raw, data=data)

    monkeypatch.setattr(Clip, "raw_frame", frame)
    settings = RawCalibration.model_validate({"mount_crop": "64x32", "mount_proxy": proxy})
    with Sequence(tiny_raw, raw_settings=settings) as cropped, Sequence(tiny_raw) as whole:
        info = cropped.frame_info(1)
        assert (info.width, info.height) == (64 // factor, 32 // factor)
        assert cropped.reference.original_width == 128
        assert cropped.reference.original_height == 64
        assert (cropped.reference.sensor_left, cropped.reference.sensor_top) == (0, 0)
        assert info.black_levels == whole.frame_info(1).black_levels
        assert info.as_shot_neutral == whole.frame_info(1).as_shot_neutral
        assert cropped.sources == whole.sources
        assert cropped.rate == whole.rate
        expected = cropped.clip.raw_frame(1).data[:32, :64]
        if factor > 1:
            expected = reduce_cfa(expected, cropped.clip.metadata.sensor_arrangement, factor)
        name = cropped.names()[1]
        count = info.width * info.height * 2
        pixels = cropped.read(name, cropped.size(name) - count, count)
        np.testing.assert_array_equal(np.frombuffer(pixels, "<u2").reshape(info.height, info.width), expected)
        assert cropped.read("tiny.wav", 0, cropped.size("tiny.wav")) == whole.read(
            "tiny.wav", 0, whole.size("tiny.wav")
        )


@pytest.mark.parametrize(
    "crop", ["129x32", "64x65", "0x32", "64x0", "-2x32", "64x32+1+2", "64x32+2", "64x32-2-2"]
)
def test_custom_mount_crop_rejects_odd_empty_or_malformed_syntax(crop: str) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    with pytest.raises(ValueError, match="crop"):
        RawCalibration(mount_crop=crop)


def test_custom_mount_crop_bounds_and_proxy_alignment_are_checked_before_serving(tiny_raw: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    with pytest.raises(ValueError, match="does not fit the frame"):
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_crop="256x32"))
    with pytest.raises(ValueError, match="does not fit the frame"):
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_crop="64x32+80+0"))
    with pytest.raises(ValueError, match="multiples of 16"):
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_crop="62x32", mount_proxy="eighth"))
    with pytest.raises(ValueError, match="multiples of 16"):
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_crop="64x32+8+0", mount_proxy="eighth"))


def test_a_placed_mount_crop_serves_that_part_of_the_sensor(tiny_raw: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    with Sequence(tiny_raw, raw_settings=RawCalibration(mount_crop="64x32+32+16")) as cropped:
        info = cropped.frame_info(1)
        assert (info.width, info.height) == (64, 32)
        assert (cropped.reference.sensor_left, cropped.reference.sensor_top) == (32, 16)
        name = cropped.names()[1]
        count = info.width * info.height * 2
        pixels = cropped.read(name, cropped.size(name) - count, count)
        expected = cropped.clip.raw_frame(1).data[16:48, 32:96]
        np.testing.assert_array_equal(np.frombuffer(pixels, "<u2").reshape(32, 64), expected)


def test_mount_level_preflight_uses_effective_policy(tiny_raw: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    fixed = RawCalibration(black_level=2000, white_level=1000)
    with pytest.raises(ValueError, match="white level must be above"):
        Sequence(tiny_raw, raw_settings=fixed)
    with Sequence(tiny_raw, raw_settings=fixed.model_copy(update={"level_policy": "recorded"})) as recorded:
        assert recorded.frame_info(0).white_level > max(recorded.frame_info(0).black_levels)


@pytest.mark.parametrize("shading", ["opcode", "baked"])
@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize(
    ("preset", "offset"),
    [("Panasonic Varicam RAW", -2), ("Blackmagic Pocket Cinema Camera 4K", 0)],
)
def test_camera_presets_keep_sensor_data_and_add_compatible_exposure(
    tiny_raw: Path, preset: str, offset: int, unique: bool, shading: str
) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    with (
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_shading=shading)) as plain,
        Sequence(
            tiny_raw,
            raw_settings=RawCalibration(
                mount_shading=shading,
                mount_camera_model=preset,
                mount_unique_model=unique,
            ),
        ) as selected,
    ):
        normal, changed = plain.frame_info(0), selected.frame_info(0)
        assert changed.unique_model == preset + (f" {tiny_raw.stem}" if unique else "")
        assert changed.baseline_exposure == normal.baseline_exposure + offset
        assert changed.colour_matrix_1 == normal.colour_matrix_1
        assert changed.colour_matrix_2 == normal.colour_matrix_2
        assert changed.as_shot_neutral == normal.as_shot_neutral
        assert changed.black_levels == normal.black_levels
        assert changed.white_level == normal.white_level
        assert changed.gain_maps == normal.gain_maps


def test_time_of_day_timecode_is_written_per_frame_and_in_the_wav(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    import struct

    from mcrawsome.io.timecode import clip_timecode

    t = np.arange(48000) / 48000
    tone = (4000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    source = make_mcraw(
        tmp_path / "clock.mcraw", [i * 41_666_667 for i in range(24)], audio=np.stack([tone, tone], -1)
    )

    def served(sequence: Sequence, name: str) -> bytes:
        return b"".join(sequence.read(name, at, 4093) for at in range(0, sequence.size(name), 4093))

    with Sequence(source) as plain, Sequence(source, timecode="UTC") as timed, Clip(source) as clip:
        code = clip_timecode(clip, "UTC", float(timed.rate))
        assert timed.timecode == code
        for number in (0, 5, 23):
            name = plain.names()[number]
            assert timed.size(name) == len(served(timed, name))
            frame = tmp_path / "timed.dng"
            frame.write_bytes(served(timed, name))
            assert bytes(read_dng_tags(frame)[51043]) == code.address(number)
            frame.write_bytes(served(plain, name))
            assert 51043 not in read_dng_tags(frame)
            assert served(timed, name)[-128 * 64 * 2 :] == served(plain, name)[-128 * 64 * 2 :]
        wav = plain.names()[-1]
        timed_wav, plain_wav = served(timed, wav), served(plain, wav)
        assert timed_wav[12:16] == b"bext"
        reference = struct.unpack_from("<Q", timed_wav, 12 + 8 + 338)[0]
        assert reference == code.time_reference(48000)
        with wave.open(io.BytesIO(timed_wav)) as left, wave.open(io.BytesIO(plain_wav)) as right:
            assert left.readframes(left.getnframes()) == right.readframes(right.getnframes())
    with Sequence(source, timecode="UTC", frame_range=(12, 6)) as trimmed, Clip(source) as clip:
        assert trimmed.timecode == clip_timecode(clip, "UTC", float(trimmed.rate), 12 / float(trimmed.rate))
