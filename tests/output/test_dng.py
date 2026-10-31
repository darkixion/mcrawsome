import json
import struct
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import rawpy
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.camera.shading import shading_gains
from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata
from mcrawsome.output.dng import dng_info, gain_map_opcodes, parse_gain_maps, read_dng_tags, write_dng

DATA = Path(__file__).parent.parent / "data"


def _frame(width: int = 1918, height: int = 1078, orientation: int = 0) -> FrameMetadata:
    rng = np.random.default_rng(2)
    lw, lh = 17, 13
    yy, xx = np.mgrid[0:lh, 0:lw]
    bowl = 1 + 2.0 * (((xx - lw / 2) / lw) ** 2 + ((yy - lh / 2) / lh) ** 2)
    planes = [(bowl * (1 + 0.05 * c) + rng.normal(0, 0.01, bowl.shape)).ravel().tolist() for c in range(4)]
    return FrameMetadata.model_validate(
        {
            "width": width, "height": height, "originalWidth": 4080, "originalHeight": 3072,
            "orientation": orientation, "compressionType": 7, "asShotNeutral": [0.5, 1.0, 0.7],
            "dynamicBlackLevel": [64.0, 64.0, 65.0, 64.0], "dynamicWhiteLevel": 4095.0, "iso": 100.0,
            "exposureTime": 1e7, "lensShadingMap": planes, "lensShadingMapWidth": lw,
            "lensShadingMapHeight": lh, "recvdTimestampMs": 0,
        }
    )  # fmt: skip


def _container() -> object:
    return parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))


def _write(tmp_path: Path, frame: FrameMetadata) -> tuple[Path, np.ndarray]:
    rng = np.random.default_rng(4)
    data = rng.integers(64, 4095, (frame.height, frame.width), dtype=np.uint16)
    info = dng_info(_container(), frame, Fraction(24000, 1001), exposure_stops=0.5)  # type: ignore[arg-type]
    path = tmp_path / "f.dng"
    write_dng(path, data, info)
    return path, data


def test_tags_round_trip(tmp_path: Path) -> None:
    frame = _frame()
    path, _ = _write(tmp_path, frame)
    tags = read_dng_tags(path)
    assert tags[256] == (1918,)
    assert tags[257] == (1078,)
    assert tags[262] == (32803,)
    assert tags[50706] == (1, 4, 0, 0)
    assert tags[50717] == (4095,)
    assert tags[50714] == pytest.approx((64.0, 64.0, 65.0, 64.0))
    assert tags[50728] == pytest.approx((0.5, 1.0, 0.7))
    assert tags[50730] == pytest.approx((0.5,))
    assert tags[51044] == pytest.approx((24000 / 1001,))
    assert tags[274] == (1,)
    assert tags[279] == (1918 * 1078 * 2,)


def test_upside_down_frames_are_tagged(tmp_path: Path) -> None:
    path, _ = _write(tmp_path, _frame(orientation=3))
    assert read_dng_tags(path)[274] == (3,)


@pytest.mark.parametrize(("width", "height"), [(1918, 1078), (3840, 2160)])
def test_gain_maps_reproduce_the_shading_model(width: int, height: int) -> None:
    frame = _frame(width, height)
    maps = parse_gain_maps(gain_map_opcodes(frame, "rggb"))
    assert len(maps) == 4
    expected = shading_gains(frame)  # R, G on R rows, G on B rows, B, at half resolution
    for gain_map, plane in zip(maps, (expected[0], expected[1], expected[2], expected[3]), strict=True):
        ys = (np.arange(height // 2) * 2 + gain_map.top + 0.5) / height
        xs = (np.arange(width // 2) * 2 + gain_map.left + 0.5) / width
        got = gain_map.evaluate(ys, xs)
        assert got.shape == plane.shape
        # OpenCV 4.10 quantises remap coordinates; the DNG evaluates the exact bilinear grid.
        relative = np.abs(got - plane) / plane
        assert np.mean(relative > 2e-3) < 0.001
        np.testing.assert_allclose(got, plane, rtol=2.5e-3)


def test_libraw_reads_what_was_written(tmp_path: Path) -> None:
    path, data = _write(tmp_path, _frame())
    with rawpy.imread(str(path)) as raw:
        np.testing.assert_array_equal(raw.raw_image, data)
        assert raw.white_level == 4095
        pattern = raw.raw_pattern
        assert pattern is not None
        black = (64.0, 64.0, 65.0, 64.0)  # by position, row by row
        for y in range(2):
            for x in range(2):
                assert raw.black_level_per_channel[pattern[y, x]] == black[y * 2 + x]
        cfa = _container().sensor_arrangement  # type: ignore[attr-defined]
        names = "".join("rgbg"[pattern[y, x]] for y in range(2) for x in range(2))
        assert names == cfa
        wb = np.array(raw.camera_whitebalance[:3])
        np.testing.assert_allclose(wb / wb[1], [2.0, 1.0, 1 / 0.7], rtol=1e-3)


@pytest.mark.fixture
def test_dng_export(fixture_10bit_rotated: Path, tmp_path: Path) -> None:
    import tomllib
    import wave

    from mcrawsome.output.dng_export import export_dng
    from mcrawsome.recipe.load import build_recipe

    folder = tmp_path / "clip_DNG"
    calls: list[tuple[int, int]] = []
    result = export_dng(
        fixture_10bit_rotated,
        folder,
        build_recipe([{"output": {"kind": "dng"}}]),
        workers=2,
        progress=lambda phase, done, total: calls.append((done, total)) if phase == "rendering" else None,
    )
    files = sorted(folder.glob("*.dng"))
    assert [f.name for f in files] == [f"clip_10bit_rotated-{i:06d}.dng" for i in range(3)]
    assert calls[-1] == (3, 3)
    assert read_dng_tags(files[0])[274] == (3,)
    with rawpy.imread(str(files[-1])) as raw:
        assert raw.raw_image.shape == (2160, 3840)
    with wave.open(str(folder / "clip_10bit_rotated.wav")) as w:
        seconds = w.getnframes() / w.getframerate()
    assert abs(seconds - 3 / 24) < 0.01
    side = tomllib.loads(result.sidecar.read_text())
    assert side["measurements"]["frames"] == 3
    assert result.sidecar == tmp_path / "clip_DNG.mcrawsome.toml"
    assert not list(tmp_path.glob(".mcrawsome-*"))


def test_dng_export_will_not_replace_a_folder_with_other_files(tmp_path: Path) -> None:
    from mcrawsome.output.dng_export import export_dng
    from mcrawsome.output.export import ExportError
    from mcrawsome.recipe.load import build_recipe

    folder = tmp_path / "clip_DNG"
    folder.mkdir()
    (folder / "notes.txt").write_text("mine")
    recipe = build_recipe([{"output": {"kind": "dng"}}])
    with pytest.raises(ExportError, match="already exists"):
        export_dng(tmp_path / "missing.mcraw", folder, recipe)
    with pytest.raises(ExportError, match=r"notes\.txt"):
        export_dng(tmp_path / "missing.mcraw", folder, recipe, force=True)
    assert (folder / "notes.txt").exists()


def test_calibration_illuminants_and_noise_profile(tmp_path: Path) -> None:
    from mcrawsome.io.metadata import parse_container_metadata

    container = parse_container_metadata(json.loads((DATA / "container_metadata_174122.json").read_text()))
    frame = FrameMetadata.model_validate(json.loads((DATA / "frame_metadata_174122.json").read_text()))
    data = np.zeros((frame.height, frame.width), np.uint16)
    path = tmp_path / "c.dng"
    write_dng(path, data, dng_info(container, frame, Fraction(24), 0.0))
    tags = read_dng_tags(path)
    assert container.calibration_matrix_1 is not None
    assert tags[50723] == pytest.approx(tuple(container.calibration_matrix_1), abs=1e-6)
    assert 50724 in tags
    assert tags[50778] == (17,)  # standard A
    assert tags[50779] == (21,)  # D65
    noise = tags[51041]
    assert len(noise) == 6  # (scale, offset) for R, G, B
    assert noise[0] == pytest.approx(frame.noise_profile[0])


def test_gain_maps_follow_the_lens_shading_strength() -> None:
    frame = _frame()
    full = parse_gain_maps(gain_map_opcodes(frame, "rggb"))
    half = parse_gain_maps(gain_map_opcodes(frame, "rggb", strength=0.5))
    none = parse_gain_maps(gain_map_opcodes(frame, "rggb", strength=0.0))
    for f, h, n in zip(full, half, none, strict=True):
        np.testing.assert_allclose(h.gains, np.sqrt(f.gains), atol=1e-6)
        np.testing.assert_allclose(n.gains, 1.0, atol=1e-6)


@given(
    pattern=st.sampled_from(["rggb", "grbg", "gbrg", "bggr"]),
    height=st.integers(4, 67),
    width=st.integers(4, 67),
    seed=st.integers(0, 2**32 - 1),
)
def test_half_cfa_averages_each_bayer_site(pattern: str, height: int, width: int, seed: int) -> None:
    from mcrawsome.output.dng import half_cfa

    data = np.random.default_rng(seed).integers(0, 65536, (height, width), dtype=np.uint16)
    original = data.copy()
    result = half_cfa(data, pattern)
    assert result.dtype == np.uint16
    assert result.shape == (height // 4 * 2, width // 4 * 2)
    np.testing.assert_array_equal(data, original)
    for i, _colour in enumerate(pattern):
        y, x = divmod(i, 2)
        plane = data[y : height // 4 * 4 : 2, x : width // 4 * 4 : 2].astype(np.float64)
        expected = np.rint(plane.reshape(height // 4, 2, width // 4, 2).mean(axis=(1, 3)))
        np.testing.assert_array_equal(result[y::2, x::2], expected)


@pytest.mark.parametrize("pattern", ["rggb", "grbg", "gbrg", "bggr"])
@pytest.mark.parametrize(("width", "height"), [(128, 124), (130, 126)])
def test_half_dng_scales_metadata_and_keeps_the_pattern(
    tmp_path: Path, pattern: str, width: int, height: int
) -> None:
    frame = _frame(width, height).model_copy(update={"noise_profile": [0.04, 0.008] * 4})
    container = _container().model_copy(update={"sensor_arrangement": pattern})  # type: ignore[attr-defined]
    info = dng_info(container, frame, Fraction(24), 0.5)
    data = np.zeros((height, width), np.uint16)
    for i in range(4):
        y, x = divmod(i, 2)
        data[y::2, x::2] = 100 * (i + 1)
    path = tmp_path / "half.dng"
    write_dng(path, data, info, scale="half")
    tags = read_dng_tags(path)
    assert tags[256] == (64,)
    assert tags[257] == (62,)
    assert tags[50720] == (64, 62)  # DefaultCropSize
    assert tags[50829] == (0, 0, 62, 64)  # ActiveArea
    assert tags[50714] == pytest.approx(frame.black_level)
    assert tags[51041] == pytest.approx((0.01, 0.002) * 3)
    full_maps = parse_gain_maps(info.gain_maps)
    half_maps = parse_gain_maps(tags[51009])
    for i, (full, half) in enumerate(zip(full_maps, half_maps, strict=True)):
        assert (half.top, half.left) == divmod(i, 2)
        np.testing.assert_array_equal(half.gains, full.gains)
        assert half.spacing_v == pytest.approx(full.spacing_v * height / 124)
        assert half.spacing_h == pytest.approx(full.spacing_h * width / 128)
        assert half.origin_v == pytest.approx(full.origin_v * height / 124)
        assert half.origin_h == pytest.approx(full.origin_h * width / 128)
        # Each opcode applies to the entire reduced plane, with a two-pixel Bayer pitch.
        offset = 4 + i * (16 + 76 + half.points_v * half.points_h * 4) + 16
        assert struct.unpack_from(">8I", tags[51009], offset) == (*divmod(i, 2), 62, 64, 0, 1, 2, 2)
    with rawpy.imread(str(path)) as raw:
        assert raw.raw_image.shape == (62, 64)
        assert raw.raw_pattern is not None
        names = "".join("rgbg"[raw.raw_pattern[y, x]] for y in range(2) for x in range(2))
        assert names == pattern
        for i in range(4):
            y, x = divmod(i, 2)
            np.testing.assert_array_equal(raw.raw_image[y::2, x::2], 100 * (i + 1))


@pytest.mark.fixture
def test_half_fixture_demosaic_matches_downscaled_full(fixture_10bit_rotated: Path, tmp_path: Path) -> None:
    import cv2

    from mcrawsome.io.clip import Clip
    from mcrawsome.output.dng import half_cfa

    with Clip(fixture_10bit_rotated) as clip:
        frame = clip.raw_frame(0)
        info = dng_info(clip.metadata, frame.metadata, Fraction(24), 0.0)
    images = []
    for scale in ("full", "half"):
        path = tmp_path / f"{scale}.dng"
        write_dng(path, frame.data, info, scale=scale)
        with rawpy.imread(str(path)) as raw:
            if scale == "half":
                np.testing.assert_array_equal(raw.raw_image, half_cfa(frame.data, info.cfa))
            images.append(
                raw.postprocess(
                    use_camera_wb=True,
                    no_auto_bright=True,
                    gamma=(1, 1),
                    output_bps=16,
                    user_flip=0,
                )
            )
    full, half = images
    downscaled = cv2.resize(full, (half.shape[1], half.shape[0]), interpolation=cv2.INTER_AREA)
    error = np.abs(half.astype(np.float64) - downscaled.astype(np.float64)).mean() / 32768
    assert error < 0.02


def test_half_dng_notes_the_crop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Callable, Iterable, Iterator
    from contextlib import nullcontext
    from types import SimpleNamespace

    from mcrawsome.output import dng_export
    from mcrawsome.output.dng_export import export_dng
    from mcrawsome.output.export import ClipFacts
    from mcrawsome.recipe.load import build_recipe
    from mcrawsome.recipe.schema import OutputParams

    assert OutputParams().dng_scale == "full"
    field = OutputParams.model_fields["dng_scale"]
    assert field.json_schema_extra == {"layer": 2}
    assert field.description is not None
    assert field.description.startswith("half makes CinemaDNG proxies at half the width and height")
    clip = tmp_path / "clip.mcraw"
    clip.write_bytes(b"test")
    facts = ClipFacts(
        width=18,
        height=14,
        neutral=(0.5, 1.0, 0.7),
        measured_fps=24.0,
        frame_count=1,
        track=None,
        first_frame_ns=0,
    )
    monkeypatch.setattr(dng_export, "read_clip_facts", lambda *_: facts)

    def write(*args: object) -> int:
        settings = args[3]
        assert isinstance(settings, dng_export.DngSettings)
        write_dng(
            Path(str(args[1])) / "clip-000000.dng",
            np.zeros((14, 18), np.uint16),
            dng_info(_container(), _frame(18, 14), Fraction(24), 0.0),  # type: ignore[arg-type]
            scale=settings.scale,
        )
        return 0

    def write_all(
        _pool: object, fn: Callable[[int], int], items: Iterable[int], window: int
    ) -> Iterator[int]:
        yield from map(fn, items)

    monkeypatch.setattr(
        dng_export,
        "Clip",
        lambda _, **kwargs: nullcontext(SimpleNamespace(recovery_note=None, remosaic_note=None)),
    )
    monkeypatch.setattr(dng_export, "worker_pool", lambda _: nullcontext())
    monkeypatch.setattr(dng_export, "bounded_imap", write_all)
    monkeypatch.setattr(dng_export, "_write_frame", write)
    recipe = build_recipe([{"output": {"kind": "dng", "dng_scale": "half"}, "develop": {"exposure": 0}}])
    result = export_dng(clip, tmp_path / "clip_DNG_half", recipe, workers=1)
    assert "half-resolution DNG: cropped 18x14 to 16x12 before averaging" in result.warnings


def test_gain_maps_in_colour_mode_carry_the_colour_only_map() -> None:
    from mcrawsome.camera.shading import lens_map

    frame = _frame()
    maps = parse_gain_maps(gain_map_opcodes(frame, "rggb", mode="colour"))
    colour = lens_map(frame, "colour")
    for gain_map, plane in zip(maps, (colour[0], colour[1], colour[2], colour[3]), strict=True):
        np.testing.assert_allclose(gain_map.gains, plane, atol=1e-6)
    assert gain_map_opcodes(frame, "rggb") == gain_map_opcodes(frame, "rggb", mode="full")


def test_dngs_in_colour_shading_mode(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    from mcrawsome.output.dng_export import export_dng
    from mcrawsome.recipe.load import build_recipe

    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"output": {"kind": "dng"}, "develop": {"lens_shading_mode": "colour"}}])
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    maps = parse_gain_maps(read_dng_tags(min(result.output.glob("*.dng")))[51009])
    assert max(float(m.gains.max()) for m in maps) <= 1.0 + 1e-6


@pytest.mark.parametrize("factor", [4, 8])
@pytest.mark.parametrize("pattern", ["rggb", "gbrg"])
def test_reduce_cfa_averages_factor_squared_samples_per_site(factor: int, pattern: str) -> None:
    from mcrawsome.output.dng import reduce_cfa

    height, width = 4 * factor + 3, 6 * factor + 1  # incomplete blocks at the bottom and right are cropped
    data = np.random.default_rng(factor).integers(0, 65536, (height, width), dtype=np.uint16)
    result = reduce_cfa(data, pattern, factor)
    block = 2 * factor
    assert result.shape == (height // block * 2, width // block * 2)
    for i in range(4):
        y, x = divmod(i, 2)
        plane = data[y : height // block * block : 2, x : width // block * block : 2].astype(np.float64)
        expected = np.rint(plane.reshape(height // block, factor, width // block, factor).mean(axis=(1, 3)))
        np.testing.assert_array_equal(result[y::2, x::2], expected)


def test_reduce_cfa_by_two_is_the_half_cfa() -> None:
    from mcrawsome.output.dng import half_cfa, reduce_cfa

    data = np.random.default_rng(3).integers(0, 4096, (38, 46), dtype=np.uint16)
    np.testing.assert_array_equal(reduce_cfa(data, "rggb", 2), half_cfa(data, "rggb"))


@pytest.mark.parametrize(("scale", "factor"), [("quarter", 4), ("eighth", 8)])
def test_quarter_and_eighth_dngs_scale_metadata(tmp_path: Path, scale: str, factor: int) -> None:
    width, height = 64 * factor, 32 * factor  # LibRaw refuses images much smaller than 64x32
    frame = _frame(width, height).model_copy(update={"noise_profile": [0.04, 0.008] * 4})
    info = dng_info(_container(), frame, Fraction(24), 0.5)  # type: ignore[arg-type]
    path = tmp_path / f"{scale}.dng"
    write_dng(path, np.full((height, width), 500, np.uint16), info, scale=scale)  # type: ignore[arg-type]
    tags = read_dng_tags(path)
    assert (tags[256], tags[257]) == ((width // factor,), (height // factor,))
    assert tags[50720] == (width // factor, height // factor)
    assert tags[51041] == pytest.approx(tuple(v / factor**2 for v in (0.04, 0.008)) * 3)
    for full, small in zip(parse_gain_maps(info.gain_maps), parse_gain_maps(tags[51009]), strict=True):
        assert small.spacing_h == pytest.approx(full.spacing_h)  # the frame divides exactly: same geometry
        assert small.origin_v == pytest.approx(full.origin_v)
    with rawpy.imread(str(path)) as raw:
        assert raw.raw_image.shape == (height // factor, width // factor)


def test_quarter_dng_export_names_its_crop(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    from mcrawsome.output.dng_export import export_dng
    from mcrawsome.recipe.load import build_recipe

    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"output": {"kind": "dng", "dng_scale": "quarter"}}])
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    assert (tags[256], tags[257]) == ((32,), (16,))
    eighth = build_recipe([{"output": {"kind": "dng", "dng_scale": "eighth"}}])
    result = export_dng(clip, tmp_path / "eighth_DNG", eighth, workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    assert (tags[256], tags[257]) == ((16,), (8,))
    assert not any("cropped" in note for note in result.warnings)


def test_lossless_white_override_preserves_samples_above_white(tmp_path: Path) -> None:
    import dataclasses

    import rawpy

    from mcrawsome.output.dng import dng_info

    info = dataclasses.replace(dng_info(_container(), _frame(128, 64), Fraction(24), 0.0), white_level=127)  # type: ignore[arg-type]
    data = np.full((info.height, info.width), 1000, np.uint16)
    path = tmp_path / "lower-white.dng"
    write_dng(path, data, info, "lossless")
    assert read_dng_tags(path)[50717] == (127,)
    with rawpy.imread(str(path)) as raw:
        np.testing.assert_array_equal(raw.raw_image, data)


@pytest.mark.parametrize(
    ("compression", "scale", "digest"),
    [
        ("none", "full", "da8b5acf44f2836895a6807b1f7bf8b0514208a15b40679cb5195b83714f26e5"),
        ("lossless", "full", "c3ffcfadc43e0801a5b94da7a792269b84f7047f10ebdc1e9b830f6f74f4ddba"),
        ("none", "half", "1f3dd499483c66f82f0e7c0d43f0f8c9c0ff2d402afaee4e915ce3c75314f4ac"),
    ],
)
def test_cinemadng_bytes_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compression: str, scale: str, digest: str
) -> None:
    """Recorded before the TIFF layout moved to output/tiff.py and stills gained DNG options, and again for
    the rename to MCRAWsome (the Software tag alone changed)."""
    import hashlib

    from conftest import still_container, still_frame

    from mcrawsome.output import dng

    monkeypatch.setattr(dng, "__version__", "0.1.0")
    data = np.random.default_rng(11).integers(64, 4095, (64, 96), dtype=np.uint16)
    info = dng_info(still_container("gbrg"), still_frame(96, 64), Fraction(24000, 1001), exposure_stops=0.5)
    path = tmp_path / "f.dng"
    write_dng(path, data, info, compression, scale=scale)  # type: ignore[arg-type]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_gain_maps_from_planes_reproduce_the_planes() -> None:
    from mcrawsome.output.dng import gain_map_planes

    height, width = 120, 160
    yy, xx = np.mgrid[0 : height // 2, 0 : width // 2].astype(np.float32)
    planes = tuple(
        (1 + 0.5 * ((xx - 40) / 40) ** 2 + 0.3 * ((yy - 30) / 30) ** 2) * (1 + 0.1 * c) for c in range(4)
    )
    maps = parse_gain_maps(gain_map_planes(planes, width, height, "grbg"))
    assert len(maps) == 4
    sites = ((0, 0), (0, 1), (1, 0), (1, 1))
    expected = {"g0": planes[1], "r": planes[0], "b": planes[3], "g1": planes[2]}  # grbg: G on R rows first
    for gain_map, key in zip(maps, ("g0", "r", "b", "g1"), strict=True):
        y0, x0 = sites[maps.index(gain_map)]
        ys = (np.arange(height // 2) * 2 + y0 + 0.5) / height
        xs = (np.arange(width // 2) * 2 + x0 + 0.5) / width
        np.testing.assert_allclose(gain_map.evaluate(ys, xs), expected[key], rtol=5e-3)
