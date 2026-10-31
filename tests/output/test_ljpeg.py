import json
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import rawpy

from mcrawsome._raw import encode_ljpeg
from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata
from mcrawsome.output.dng import dng_info, read_dng_tags, write_dng

DATA = Path(__file__).parent.parent / "data"


def _info(width: int, height: int, white: int) -> object:
    frame = FrameMetadata.model_validate(
        {
            "width": width, "height": height, "originalWidth": width, "originalHeight": height,
            "orientation": 0, "compressionType": 7, "asShotNeutral": [0.5, 1.0, 0.7],
            "dynamicBlackLevel": [64.0] * 4, "dynamicWhiteLevel": float(white), "iso": 100.0,
            "exposureTime": 1e7, "lensShadingMap": [[1.0] * 4] * 4, "lensShadingMapWidth": 2,
            "lensShadingMapHeight": 2, "recvdTimestampMs": 0,
        }
    )  # fmt: skip
    container = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
    return dng_info(container, frame, Fraction(24), exposure_stops=0.0)


def _cfa(width: int, height: int, bits: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    top = (1 << bits) - 1
    gradient = (xx / max(width - 1, 1) * 0.6 + yy / max(height - 1, 1) * 0.3) * top
    noisy = gradient + rng.normal(0, 0.02 * top, (height, width))
    return np.asarray(np.clip(noisy, 0, top), dtype=np.uint16)


def _round_trip(tmp_path: Path, cfa: np.ndarray, bits: int) -> np.ndarray:
    path = tmp_path / "f.dng"
    write_dng(path, cfa, _info(cfa.shape[1], cfa.shape[0], (1 << bits) - 1), compression="lossless")  # type: ignore[arg-type]
    with rawpy.imread(str(path)) as raw:
        return np.array(raw.raw_image)


@pytest.mark.parametrize("bits", [10, 12])
# LibRaw misreads images narrower than one tile and, it seems, exactly 512 wide (any content): sizes
# here avoid both; real frames are 3840 wide.
@pytest.mark.parametrize(("width", "height"), [(256, 256), (300, 62), (600, 300), (1024, 1024)])
def test_lossless_dng_decodes_exactly(tmp_path: Path, bits: int, width: int, height: int) -> None:
    cfa = _cfa(width, height, bits)
    np.testing.assert_array_equal(_round_trip(tmp_path, cfa, bits), cfa)


@pytest.mark.parametrize("value", [0, 4095])
def test_flat_tiles_decode_exactly(tmp_path: Path, value: int) -> None:
    cfa = np.full((256, 768), value, np.uint16)
    np.testing.assert_array_equal(_round_trip(tmp_path, cfa, 12), cfa)


def test_the_tags_describe_tiles_and_lossless_jpeg(tmp_path: Path) -> None:
    path = tmp_path / "f.dng"
    write_dng(path, _cfa(600, 300, 12), _info(600, 300, 4095), compression="lossless")  # type: ignore[arg-type]
    tags = read_dng_tags(path)
    assert tags[259] == (7,)
    assert tags[322] == (256,)
    assert tags[323] == (256,)
    assert len(tags[324]) == len(tags[325]) == 3 * 2  # 3 tiles across, 2 down
    assert 273 not in tags


def test_a_tile_is_a_lossless_jpeg_stream() -> None:
    data = encode_ljpeg(_cfa(256, 256, 12), 12)
    assert data[:2] == b"\xff\xd8"  # SOI
    assert data[2:4] == b"\xff\xc4" or data[2:4] == b"\xff\xc3"  # DHT or SOF3 first
    assert b"\xff\xc3" in data
    assert data[-2:] == b"\xff\xd9"  # EOI


@pytest.mark.fixture
def test_a_real_frame_shrinks_and_decodes_exactly(fixture_12bit: Path, tmp_path: Path) -> None:
    from mcrawsome.io.clip import Clip

    with Clip(fixture_12bit) as clip:
        raw = clip.raw_frame(0)
        info = dng_info(clip.metadata, raw.metadata, Fraction(24), 0.0)
    path = tmp_path / "real.dng"
    write_dng(path, raw.data, info, compression="lossless")
    with rawpy.imread(str(path)) as decoded:
        np.testing.assert_array_equal(decoded.raw_image, raw.data)
    assert path.stat().st_size < 0.7 * raw.data.nbytes


def test_samples_above_the_precision_are_refused() -> None:
    tile = np.zeros((16, 16), np.uint16)
    tile[3, 5] = 4096
    with pytest.raises(ValueError, match="precision"):
        encode_ljpeg(tile, 12)
    tile[3, 5] = 4095
    assert encode_ljpeg(tile, 12)[:2] == b"\xff\xd8"


def test_sixteen_bit_tiles_decode_exactly(tmp_path: Path) -> None:
    cfa = _cfa(600, 300, 16)
    np.testing.assert_array_equal(_round_trip(tmp_path, cfa, 16), cfa)


def test_a_difference_of_32768_decodes_exactly(tmp_path: Path) -> None:
    cfa = np.zeros((256, 768), np.uint16)
    cfa[:, 2::4] = 32768  # each component's samples alternate 0, 32768: category 16 differences
    cfa[:, 3::4] = 32768
    cfa[100:, :] = 65535 - cfa[100:, :]
    np.testing.assert_array_equal(_round_trip(tmp_path, cfa, 16), cfa)
