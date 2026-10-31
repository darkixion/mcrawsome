"""Full-sensor crops can retain Quad Bayer photosites despite needRemosaic being false."""

import json
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from conftest import _DATA, SYNTHETIC_STEP

from mcrawsome import _raw
from mcrawsome.camera.remosaic import quad_colours, remosaic
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerWriter
from mcrawsome.mount.sequence import Sequence

Mosaic = npt.NDArray[np.uint16]


@pytest.fixture
def unflagged_quad(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Mosaic, list[int]]:
    container = json.loads((_DATA / "container_metadata_174122.json").read_text())
    container["sensorArrangment"] = "gbrg"
    frame = json.loads((_DATA / "frame_metadata_174122.json").read_text())
    frame |= {
        "width": 128,
        "height": 64,
        "originalWidth": 272,
        "originalHeight": 192,
        "rowStride": 256,
        "orientation": 0,
        "isBinned": False,
        "needRemosaic": False,
    }
    path = tmp_path / "unflagged.mcraw"
    with ContainerWriter(path, container) as writer:
        for index in range(3):
            stamp = index * SYNTHETIC_STEP
            writer.add_frame(stamp, bytes([index]), json.dumps(frame | {"timestamp": stamp}).encode())
    raw = np.array([600, 220, 390, 600], np.uint16)[quad_colours("gbrg", 64, 128)]
    decoded: list[int] = []

    def decode(payload: bytes, width: int, height: int, compression: int) -> Mosaic:
        decoded.append(payload[0])
        return raw.copy() if payload[0] != 1 else np.full_like(raw, 300)

    monkeypatch.setattr(_raw, "decode", decode)
    return path, raw, decoded


def test_metadata_stays_read_only_until_pixels_are_requested(
    unflagged_quad: tuple[Path, Mosaic, list[int]],
) -> None:
    path, _, decoded = unflagged_quad
    with Clip(path, metadata_only=True) as clip:
        assert not clip.frame_metadata(0).need_remosaic
        assert decoded == []


def test_a_late_first_read_checks_frame_zero_and_keeps_the_decision(
    unflagged_quad: tuple[Path, Mosaic, list[int]],
) -> None:
    path, raw, decoded = unflagged_quad
    with Clip(path) as clip:
        actual = clip.raw_frame(2)
        expected = remosaic(raw, "gbrg", actual.metadata.black_level)
        np.testing.assert_array_equal(actual.data, expected)
        assert not np.array_equal(actual.data, raw)
        assert decoded == [2, 0]
        assert clip.remosaic_note is not None
        assert "detected" in clip.remosaic_note
        with clip.independent_reader() as other:
            other.raw_frame(1)
            assert decoded == [2, 0, 1]


def test_a_mount_serves_the_recovered_bayer_pixels(unflagged_quad: tuple[Path, Mosaic, list[int]]) -> None:
    path, raw, _ = unflagged_quad
    with Sequence(path) as sequence:
        name = sequence.names()[2]
        served = b"".join(sequence.read(name, at, 4096) for at in range(0, sequence.size(name), 4096))
        expected = remosaic(raw, "gbrg", sequence.clip.frame_metadata(0).black_level)
        assert served[-128 * 64 * 2 :] == expected.astype("<u2").tobytes()


@pytest.mark.parametrize(
    "overrides",
    [
        {"isBinned": True},
        {"isBinned": None},
        {"originalWidth": 128, "originalHeight": 64},
    ],
)
def test_ordinary_capture_metadata_does_not_trigger_inference(
    unflagged_quad: tuple[Path, Mosaic, list[int]],
    overrides: dict[str, int | bool | None],
) -> None:
    path, raw, decoded = unflagged_quad
    with Clip(path) as clip:
        container = clip.raw_metadata
        frames = [clip.raw_frame_metadata(index) | overrides for index in range(3)]
    changed = path.with_name("ordinary.mcraw")
    with ContainerWriter(changed, container) as writer:
        for index, frame in enumerate(frames):
            writer.add_frame(index * SYNTHETIC_STEP, bytes([index]), json.dumps(frame).encode())
    with Clip(changed) as clip:
        np.testing.assert_array_equal(clip.raw_frame(2).data, raw)
        assert decoded == [2]
        assert clip.remosaic_note is None


def test_starting_on_an_ambiguous_frame_still_uses_frame_zero(
    unflagged_quad: tuple[Path, Mosaic, list[int]],
) -> None:
    path, _, decoded = unflagged_quad
    with Clip(path) as clip:
        clip.raw_frame(1)
        assert decoded == [1, 0]
        assert clip._unflagged_quad is True


def test_the_global_switch_also_controls_missing_flag_recovery(
    unflagged_quad: tuple[Path, Mosaic, list[int]],
) -> None:
    from mcrawsome.server.settings import InterfaceSettings, save_settings

    path, raw, decoded = unflagged_quad
    save_settings(InterfaceSettings(quad_bayer_remosaic=False))
    with Clip(path) as clip:
        np.testing.assert_array_equal(clip.raw_frame(2).data, raw)
        assert decoded == [2]
        assert clip.remosaic_note is None
