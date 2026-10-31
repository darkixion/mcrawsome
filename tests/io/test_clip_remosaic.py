"""Frames marked as Quad Bayer (needRemosaic) are read as their Bayer mosaic everywhere."""

from pathlib import Path
from typing import Literal

import numpy as np
import pytest
from quad_clips import quad_clip, recorded_values

from mcrawsome.camera.remosaic import remosaic
from mcrawsome.io.clip import Clip
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import _CFA_CODES, read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe


@pytest.mark.parametrize("pattern", ["gbrg", "rggb", "grbg", "bggr"])
def test_a_quad_bayer_frame_is_read_as_its_bayer_mosaic(
    tmp_path: Path, pattern: Literal["rggb", "bggr", "grbg", "gbrg"]
) -> None:
    path = quad_clip(tmp_path / "quad.mcraw", pattern=pattern)
    with Clip(path) as clip:
        assert clip.metadata.sensor_arrangement == pattern
        assert clip.frame_metadata(1).need_remosaic
        frame = clip.raw_frame(1)
        recorded = recorded_values(path, 1)
        expected = remosaic(recorded, clip.metadata.sensor_arrangement, frame.metadata.black_level)
        np.testing.assert_array_equal(frame.data, expected)
        assert not np.array_equal(frame.data, recorded)
        assert not frame.metadata.need_remosaic  # the values are Bayer now
        with clip.independent_reader() as other:
            np.testing.assert_array_equal(other.raw_frame(1).data, expected)


def test_bayer_frames_are_read_as_recorded(tiny_raw: Path) -> None:
    with Clip(tiny_raw) as clip:
        assert not clip.frame_metadata(0).need_remosaic
        np.testing.assert_array_equal(clip.raw_frame(0).data, recorded_values(tiny_raw, 0))


def test_a_cinemadng_export_of_quad_bayer_frames_holds_the_bayer_mosaic(tmp_path: Path) -> None:
    path = quad_clip(tmp_path / "quad.mcraw")
    result = export_dng(path, tmp_path / "quad_DNG", build_recipe([{"output": {"kind": "dng"}}]), workers=1)
    assert any("Quad Bayer" in note for note in result.warnings)
    with Clip(path) as clip:
        for index in range(3):
            dng = result.output / f"quad-{index:06d}.dng"
            tags = read_dng_tags(dng)
            pixels = np.frombuffer(dng.read_bytes()[-128 * 64 * 2 :], "<u2").reshape(64, 128)
            np.testing.assert_array_equal(pixels, clip.raw_frame(index).data)
            assert tags[33422] == tuple(_CFA_CODES[c] for c in clip.metadata.sensor_arrangement)


def test_a_mount_of_quad_bayer_frames_serves_the_bayer_mosaic(tmp_path: Path) -> None:
    path = quad_clip(tmp_path / "quad.mcraw")
    with Sequence(path) as sequence, Clip(path) as clip:
        name = sequence.names()[2]
        served = b"".join(sequence.read(name, at, 4096) for at in range(0, sequence.size(name), 4096))
        assert served[-128 * 64 * 2 :] == clip.raw_frame(2).data.astype("<u2").tobytes()


def test_disabled_remosaic_preserves_marked_photosites_and_reader_snapshot(tmp_path: Path) -> None:
    from mcrawsome.server.settings import InterfaceSettings, save_settings

    path = quad_clip(tmp_path / "quad.mcraw")
    save_settings(InterfaceSettings(quad_bayer_remosaic=False))
    with Clip(path) as clip:
        np.testing.assert_array_equal(clip.raw_frame(0).data, recorded_values(path, 0))
        assert clip.remosaic_note is None
        save_settings(InterfaceSettings(quad_bayer_remosaic=True))
        with clip.independent_reader() as other:
            np.testing.assert_array_equal(other.raw_frame(1).data, recorded_values(path, 1))
    with Clip(path) as enabled:
        assert not np.array_equal(enabled.raw_frame(0).data, recorded_values(path, 0))


def test_explicit_remosaic_choice_overrides_the_global_preference(tmp_path: Path) -> None:
    from mcrawsome.server.settings import InterfaceSettings, save_settings

    path = quad_clip(tmp_path / "quad.mcraw")
    save_settings(InterfaceSettings(quad_bayer_remosaic=False))
    with Clip(path, quad_bayer_remosaic=True) as clip:
        raw = recorded_values(path, 0)
        expected = remosaic(raw, clip.metadata.sensor_arrangement, clip.frame_metadata(0).black_level)
        np.testing.assert_array_equal(clip.raw_frame(0).data, expected)


@pytest.mark.parametrize("pattern", ["gbrg", "rggb", "grbg", "bggr"])
def test_adaptive_quad_bayer_binning_keeps_colour_cells_and_full_reader(
    tmp_path: Path, pattern: Literal["rggb", "bggr", "grbg", "gbrg"]
) -> None:
    from mcrawsome.preview.session import PreviewSession, PreviewSettings

    path = quad_clip(tmp_path / "quad.mcraw", pattern=pattern)
    recorded = recorded_values(path, 0)
    expected = np.rint(recorded.reshape(32, 2, 64, 2).mean(axis=(1, 3))).astype(np.uint16)
    with Clip(path) as clip:
        frame = clip.draft_frame(0)
        np.testing.assert_allclose(frame.data, expected, atol=1)
        assert frame.metadata.width == 64
        assert frame.metadata.height == 32
        np.testing.assert_array_equal(
            clip.raw_frame(0).data, remosaic(recorded, pattern, frame.metadata.black_level)
        )
    with Clip(path, quad_bayer_remosaic=False) as clip:
        np.testing.assert_array_equal(clip.draft_frame(0).data, recorded)
    with PreviewSession(
        path, build_recipe([{"develop": {"exposure": 0}}]), PreviewSettings(gpu=False)
    ) as session:
        assert session.fast(0).size == (32, 16)
        assert session.source_encoder(0)().size == (128, 64)
