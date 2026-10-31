"""Exposure normalisation (deflicker): per-frame brightness, smoothing gains, and their use in exports."""

import math
import tomllib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.develop.deflicker import frame_brightness, normalising_gains, window_frames
from mcrawsome.output.chunks import WorkFolder
from mcrawsome.output.dng import read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe

BLACK, WHITE = (64.0,) * 4, 1023.0


def test_brightness_is_the_mean_of_the_unclipped_green_above_black() -> None:
    raw = np.full((64, 64), 64 + 100, np.uint16)
    raw[0::2, 0::2] = 64 + 400  # red sites (RGGB): not counted
    assert frame_brightness(raw, "rggb", BLACK, WHITE) == pytest.approx(100.0)
    raw[0::2, 1::2] = 1023  # half the green sites clipped: not counted either
    assert frame_brightness(raw, "rggb", BLACK, WHITE) == pytest.approx(100.0)


def test_a_frame_without_usable_samples_has_no_brightness() -> None:
    assert frame_brightness(np.full((64, 64), 1023, np.uint16), "rggb", BLACK, WHITE) is None
    assert frame_brightness(np.full((64, 64), 64, np.uint16), "rggb", BLACK, WHITE) is None


def _stops(values: list[float]) -> np.ndarray:
    return np.log2(np.asarray(values))


def test_alternating_exposure_is_evened_out() -> None:
    brightness = [100.0 * (2 if i % 2 else 1) for i in range(60)]
    gains = normalising_gains(brightness, [1.0] * 60, 21, 1.0)
    corrected = _stops([b * g for b, g in zip(brightness, gains, strict=True)])
    assert np.ptp(corrected[10:-10]) < 0.1  # within a twentieth of the one-stop flicker


def test_flicker_is_reduced_and_the_slow_trend_kept() -> None:
    rng = np.random.default_rng(0)
    trend = np.linspace(0, 2, 200)  # a sunset: two stops over the clip
    flicker = rng.normal(0, 0.3, 200)
    brightness = list(100 * 2 ** (trend + flicker))
    gains = normalising_gains(brightness, [1.0] * 200, 15, 1.0)
    corrected = _stops([b * g for b, g in zip(brightness, gains, strict=True)]) - np.log2(100)
    assert np.std(corrected - trend) < 0.3 * np.std(flicker)
    assert corrected[-20:].mean() - corrected[:20].mean() == pytest.approx(2.0 * 180 / 200, abs=0.25)


def test_strength_scales_the_correction_in_stops() -> None:
    brightness = [100.0 * (2 if i % 3 == 0 else 1) for i in range(30)]
    full = np.log2(normalising_gains(brightness, [1.0] * 30, 9, 1.0))
    half = np.log2(normalising_gains(brightness, [1.0] * 30, 9, 0.5))
    np.testing.assert_allclose(half, full / 2, atol=1e-9)
    assert normalising_gains(brightness, [1.0] * 30, 9, 0.0) == [1.0] * 30


def test_a_frame_without_brightness_uses_its_metadata_exposure() -> None:
    brightness: list[float | None] = [100.0] * 9
    exposures = [1.0] * 9
    brightness[4], exposures[4] = None, 2.0  # a frame exposed one stop more, with nothing measurable
    gains = normalising_gains(brightness, exposures, 9, 1.0)
    assert math.log2(gains[4]) < -0.8  # brought down by about a stop


def test_window_frames_are_odd() -> None:
    assert window_frames(1.0, 24.0) == 25
    assert window_frames(0.1, 24.0) == 3
    assert window_frames(0.01, 24.0) == 1


def test_a_deflickered_dng_export(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(6)])  # brighter every frame
    plain = export_dng(clip, tmp_path / "plain_DNG", build_recipe([{"output": {"kind": "dng"}}]), workers=1)
    recipe = build_recipe(
        [{"output": {"kind": "dng"}, "develop": {"deflicker": 1.0, "deflicker_seconds": 0.1}}]
    )
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    before = [read_dng_tags(p)[50730][0] for p in sorted(plain.output.glob("*.dng"))]
    after = [read_dng_tags(p)[50730][0] for p in sorted(result.output.glob("*.dng"))]
    assert len(set(before)) == 1
    assert len(set(np.round(after, 4))) > 1
    assert after[0] > before[0] > after[-1]  # the dark first frame is raised, the bright last one lowered
    measurements = tomllib.loads(result.sidecar.read_text())["measurements"]
    assert measurements["deflicker_max_stops"] > 0


def test_a_resumed_export_keeps_the_measured_gains(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.errors import McError
    from mcrawsome.output import dng_export

    clip = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(4)])
    recipe = build_recipe([{"output": {"kind": "dng"}, "develop": {"deflicker": 1.0}}])
    folder = tmp_path / "clip_DNG"
    with pytest.raises(McError):
        export_dng(clip, folder, recipe, workers=1, stop=lambda: True)
    stored = WorkFolder(folder).load()
    assert stored is not None
    assert stored.frame_gains is not None
    assert len(stored.frame_gains) == 4

    def measured_again(*_args: object, **_kwargs: object) -> list[float]:
        raise AssertionError("the gains are measured again")

    monkeypatch.setattr(dng_export, "measure_gains", measured_again)
    export_dng(clip, folder, recipe, workers=1)


def test_deflicker_is_off_by_default() -> None:
    assert build_recipe([{}]).develop.deflicker == 0.0


def _decoded(video: Path) -> list[bytes]:
    import subprocess

    from mcrawsome.output.ffmpeg import find_ffmpeg

    command = [
        str(find_ffmpeg()[0]),
        "-v",
        "error",
        "-i",
        str(video),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb48le",
        "-",
    ]
    out = subprocess.run(command, check=True, capture_output=True).stdout
    size = 128 * 64 * 6
    return [out[i : i + size] for i in range(0, len(out), size)]
