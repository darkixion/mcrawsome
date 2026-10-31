"""Log Transfer Curve on virtual mounts: the physical log CinemaDNG export's codes and tags."""

from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.clip import Clip
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output import dng_log
from mcrawsome.output.dng import read_dng_tags, write_dng
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.raw import RawCalibration


def _served(sequence: Sequence, index: int) -> bytes:
    name = sequence.names()[index]
    return b"".join(sequence.read(name, at, 997) for at in range(0, sequence.size(name), 997))


def _tags(path: Path, data: bytes) -> dict[int, object]:
    path.write_bytes(data)
    tags = read_dng_tags(path)
    tags.pop(273)  # the strip offset: a virtual DNG keeps its pixels on their own pages
    return tags


@pytest.mark.parametrize("reduce", ["auto", 2])
def test_a_log_mount_serves_the_physical_log_exports_codes_and_tags(
    tiny_raw: Path, tmp_path: Path, reduce: str | int
) -> None:
    recipe = build_recipe([{"output": {"kind": "dng", "dng_transfer": "log", "dng_reduce_bits": reduce}}])
    exported = export_dng(tiny_raw, tmp_path / "tiny_DNG", recipe, workers=1)
    settings = RawCalibration.model_validate({"mount_transfer": "log", "mount_reduce_bits": reduce})
    with Sequence(tiny_raw, raw_settings=settings) as sequence:
        for index in (2, 0, 1):
            physical = (exported.output / f"tiny-{index:06d}.dng").read_bytes()
            physical_tags = read_dng_tags(exported.output / f"tiny-{index:06d}.dng")
            physical_tags.pop(273)
            got = _served(sequence, index)
            virtual_tags = _tags(tmp_path / "virtual.dng", got)
            physical_tags.pop(50730)  # BaselineExposure: the export's own exposure setting
            virtual_tags.pop(50730)
            assert virtual_tags == physical_tags
            count = int(physical_tags[279][0])
            assert got[-count:] == physical[-count:]


def test_a_baked_log_mount_stores_its_wider_codes_in_two_bytes(tiny_raw: Path, tmp_path: Path) -> None:
    """Baking the vignette adds 2 bits; less 2, a 10-bit clip keeps 10 bits of codes: 16-bit samples."""
    linear = RawCalibration(mount_shading="baked")
    with (
        Sequence(tiny_raw, raw_settings=linear) as plain,
        Sequence(
            tiny_raw, raw_settings=linear.model_copy(update={"mount_transfer": "log", "mount_reduce_bits": 2})
        ) as sequence,
    ):
        assert sequence.log_bits == 10
        for index in (1, 2):
            info = plain.frame_info(index)
            values = np.frombuffer(_served(plain, index)[-info.width * info.height * 2 :], "<u2")
            path = tmp_path / "expected.dng"
            write_dng(path, values.reshape(info.height, info.width), info, log_bits=10, seed=index)
            expected = read_dng_tags(path)
            expected.pop(273)
            got = _served(sequence, index)
            assert _tags(tmp_path / "virtual.dng", got) == expected
            assert expected[258] == (16,)
            assert got[-values.nbytes :] == path.read_bytes()[-values.nbytes :]


def test_a_log_mount_of_a_marked_range_dithers_as_the_export_numbers_its_frames(tiny_raw: Path) -> None:
    settings = RawCalibration(mount_transfer="log")
    with Sequence(tiny_raw, raw_settings=settings, frame_range=(1, 2)) as sequence, Clip(tiny_raw) as clip:
        info = sequence.frame_info(2)
        bits = sequence.log_bits
        assert bits is not None
        raw = clip.raw_frame(2)
        codes = dng_log.encode(raw.data, info.white_level, bits, seed=2)  # output frame 2 of the recording
        assert _served(sequence, 1)[-codes.size :] == codes.astype(np.uint8).tobytes()


def test_repeated_frames_of_a_log_mount_are_dithered_by_their_own_number(
    tiny_raw: Path, tmp_path: Path
) -> None:
    settings = RawCalibration(mount_transfer="log")
    with Sequence(tiny_raw, raw_settings=settings) as sequence, Clip(tiny_raw) as clip:
        sequence.sources = [0, 0, 1]  # a frame repeated by frame-rate conversion
        info = sequence.frame_info(0)
        path = tmp_path / "expected.dng"
        write_dng(path, clip.raw_frame(0).data, info, log_bits=sequence.log_bits, seed=1)
        count = clip.raw_frame(0).data.size
        assert _served(sequence, 1)[-count:] == path.read_bytes()[-count:]
        assert _served(sequence, 0)[-count:] != _served(sequence, 1)[-count:]


def test_a_log_mount_with_too_few_bits_is_refused(tiny_raw: Path) -> None:
    with pytest.raises(ValueError, match="at least 6"):
        Sequence(tiny_raw, raw_settings=RawCalibration(mount_transfer="log", mount_reduce_bits=8))


def test_linear_mounts_are_the_default_and_carry_no_table(tiny_raw: Path, tmp_path: Path) -> None:
    calibration = RawCalibration()
    assert (calibration.mount_transfer, calibration.mount_reduce_bits) == ("linear", "auto")
    with Sequence(tiny_raw) as sequence:
        assert sequence.log_bits is None
        assert 50712 not in _tags(tmp_path / "linear.dng", _served(sequence, 0))


def test_reduce_bits_reads_from_text() -> None:
    assert RawCalibration.model_validate({"mount_reduce_bits": "4"}).mount_reduce_bits == 4
