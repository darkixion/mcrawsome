"""A clip's colour filter can be read differently from its metadata: as Bayer, as Quad Bayer, or with
another Bayer order."""

from pathlib import Path

import numpy as np
from quad_clips import quad_clip, recorded_values

from mcrawsome.camera.remosaic import remosaic
from mcrawsome.io.clip import Clip
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import _CFA_CODES, read_dng_tags
from mcrawsome.recipe.raw import RawCalibration


def test_a_flagged_quad_bayer_clip_can_be_read_as_recorded(tmp_path: Path) -> None:
    path = quad_clip(tmp_path / "quad.mcraw")
    with Clip(path, cfa_layout="bayer") as clip:
        np.testing.assert_array_equal(clip.raw_frame(1).data, recorded_values(path, 1))
        np.testing.assert_array_equal(clip.draft_frame(1).data, recorded_values(path, 1))
        assert clip.remosaic_note is None
        with clip.independent_reader() as other:
            np.testing.assert_array_equal(other.raw_frame(1).data, recorded_values(path, 1))


def test_an_unflagged_clip_can_be_read_as_quad_bayer(tiny_raw: Path) -> None:
    with Clip(tiny_raw, cfa_layout="quad", quad_bayer_remosaic=False) as clip:
        frame = clip.raw_frame(0)
        recorded = recorded_values(tiny_raw, 0)
        expected = remosaic(recorded, clip.metadata.sensor_arrangement, frame.metadata.black_level)
        np.testing.assert_array_equal(frame.data, expected)
        assert clip.draft_frame(0).data.shape == (recorded.shape[0] // 2, recorded.shape[1] // 2)
        assert clip.remosaic_note is not None
        assert "set for this clip" in clip.remosaic_note


def test_the_bayer_order_can_replace_wrong_metadata(tiny_raw: Path, tmp_path: Path) -> None:
    with Clip(tiny_raw) as recorded, Clip(tiny_raw, cfa_pattern="bggr") as corrected:
        assert recorded.metadata.sensor_arrangement != "bggr"
        assert corrected.metadata.sensor_arrangement == "bggr"
        np.testing.assert_array_equal(corrected.raw_frame(0).data, recorded.raw_frame(0).data)
    with Sequence(tiny_raw, raw_settings=RawCalibration(cfa_pattern="bggr")) as sequence:
        name = sequence.names()[0]
        path = tmp_path / "frame.dng"
        path.write_bytes(
            b"".join(sequence.read(name, at, 1 << 20) for at in range(0, sequence.size(name), 1 << 20))
        )
    assert list(read_dng_tags(path)[33422]) == [_CFA_CODES[c] for c in "bggr"]


def test_a_mount_reads_frames_with_the_clip_settings(tmp_path: Path) -> None:
    path = quad_clip(tmp_path / "quad.mcraw")
    with Sequence(path, raw_settings=RawCalibration(cfa_layout="bayer")) as sequence:
        name = sequence.names()[1]
        size = sequence.size(name)
        pixels = recorded_values(path, 1).astype("<u2").tobytes()
        assert sequence.read(name, size - len(pixels), len(pixels)) == pixels
