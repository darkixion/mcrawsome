"""Normalise Exposure on virtual mounts: the recorded exposure, or the brightness measured in the picture."""

import math
from pathlib import Path

import numpy as np
import pytest
from flicker_clips import flicker_clip

from mcrawsome.develop.crop import CropBox
from mcrawsome.develop.deflicker import frame_brightness, measure_gains
from mcrawsome.io.clip import Clip
from mcrawsome.mount import exposure
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import baked_white, exposure_headroom
from mcrawsome.recipe.raw import RawCalibration


def _served(sequence: Sequence, index: int) -> np.ndarray:
    name = sequence.names()[index]
    data = b"".join(sequence.read(name, at, 4096) for at in range(0, sequence.size(name), 4096))
    info = sequence.frame_info(sequence.sources[index])
    return np.frombuffer(data[-info.width * info.height * 2 :], "<u2").reshape(info.height, info.width)


def _stops(sequence: Sequence, clip: Clip) -> list[float]:
    out = []
    for index in range(len(sequence.sources)):
        info = sequence.frame_info(sequence.sources[index])
        bright = frame_brightness(_served(sequence, index), clip.metadata.sensor_arrangement,
                                  info.black_levels, info.white_level)  # fmt: skip
        assert bright is not None
        out.append(math.log2(bright))
    return out


@pytest.mark.parametrize(("saved", "mode"), [(True, "recorded"), (False, "off"), ("picture", "picture")])
def test_saved_switches_read_as_modes(saved: object, mode: str) -> None:
    assert RawCalibration.model_validate({"mount_normalise_exposure": saved}).mount_normalise_exposure == mode


def test_normalisation_is_off_by_default_with_a_one_second_window() -> None:
    settings = RawCalibration()
    assert (settings.mount_normalise_exposure, settings.mount_normalise_seconds) == ("off", 1.0)


def test_the_picture_mode_evens_out_flicker_the_metadata_does_not_record(tmp_path: Path) -> None:
    clip_path = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 12)  # a stop apart above black
    picture = RawCalibration(mount_normalise_exposure="picture")
    with (
        Sequence(clip_path, raw_settings=picture) as normalised,
        Sequence(clip_path, raw_settings=RawCalibration(mount_normalise_exposure="recorded")) as recorded,
        Clip(clip_path) as clip,
    ):
        before, after = _stops(recorded, clip), _stops(normalised, clip)
        assert np.ptp(before[6:-6]) > 0.8  # one exposure recorded for every frame: nothing to correct by
        assert np.ptp(after[6:-6]) < 0.1


def test_the_picture_gains_are_the_full_strength_deflicker_measurement(tmp_path: Path) -> None:
    clip_path = flicker_clip(tmp_path / "flicker.mcraw", [200, 340, 260, 300, 220] * 4)
    settings = RawCalibration(mount_normalise_exposure="picture", mount_normalise_seconds=0.5)
    with Sequence(clip_path, raw_settings=settings) as sequence:
        assert sequence.gains == pytest.approx(measure_gains(clip_path, 1.0, 0.5, 1))


def test_the_headroom_follows_the_largest_brightening_up_to_two_stops() -> None:
    assert exposure_headroom([0.5, 1.0, 0.9]) == 0  # darkened frames never clip
    assert exposure_headroom([1.0, 1.4]) == 1
    assert exposure_headroom([2.0, 1.0]) == 1
    assert exposure_headroom([3.0]) == 2
    assert exposure_headroom([40.0]) == 2  # a frame brightened further clips at the raised white level


def test_a_picture_mount_raises_the_white_level_by_its_headroom(tmp_path: Path) -> None:
    clip_path = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 12)
    picture = RawCalibration(mount_normalise_exposure="picture")
    with Sequence(clip_path, raw_settings=picture) as sequence, Sequence(clip_path) as plain:
        bits = exposure_headroom(sequence.gains or [])
        assert bits == 1
        info, normal = sequence.frame_info(3), plain.frame_info(3)
        assert info.gain_maps == normal.gain_maps
        assert info.white_level == baked_white(normal.black_levels, normal.white_level, bits)
        assert info.baseline_exposure == normal.baseline_exposure + bits


def test_the_picture_gains_are_measured_once_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_path = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 6)
    picture = RawCalibration(mount_normalise_exposure="picture")
    with Sequence(clip_path, raw_settings=picture) as first:
        gains = first.gains

    def forbidden(*args: object) -> None:
        raise AssertionError("measured the clip again")

    monkeypatch.setattr(exposure, "measure", forbidden)
    with Sequence(clip_path, raw_settings=picture) as again:
        assert again.gains == gains
    other = picture.model_copy(update={"mount_normalise_seconds": 2.0})
    with pytest.raises(AssertionError, match="again"):
        Sequence(clip_path, raw_settings=other)


def test_a_changed_recording_is_measured_again(tmp_path: Path) -> None:
    clip_path = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 6)
    picture = RawCalibration(mount_normalise_exposure="picture")
    with Sequence(clip_path, raw_settings=picture) as first:
        gains = first.gains
    flicker_clip(clip_path, [340, 200] * 6)
    with Sequence(clip_path, raw_settings=picture) as again:
        assert again.gains != gains


def test_a_changed_remosaic_choice_measures_the_picture_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_path = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 6)
    picture = RawCalibration(mount_normalise_exposure="picture")
    with Sequence(clip_path, raw_settings=picture, quad_bayer_remosaic=False):
        pass

    measured: list[bool] = []
    original = exposure.measure

    def record(clip: Clip, settings: RawCalibration, crop: CropBox | None) -> list[float]:
        measured.append(clip.quad_bayer_remosaic)
        return original(clip, settings, crop)

    monkeypatch.setattr(exposure, "measure", record)
    with Sequence(clip_path, raw_settings=picture, quad_bayer_remosaic=True):
        pass
    assert measured == [True]
