from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli


@pytest.mark.parametrize(
    ("options", "name"),
    [
        ((), "a_48-71.mcraw"),
        (("--start", "3"), "a_72-95.mcraw"),
        (("--duration", "2f"), "a_48-49.mcraw"),
        (("--start", "2", "--duration", "1"), "a_48-71.mcraw"),
        (("--trim.start", "3"), "a_72-95.mcraw"),
        (("--set", "trim.duration=2"), "a_48-95.mcraw"),
        (("--start", "00:00:02", "--duration", "1"), "a_48-71.mcraw"),
    ],
)
def test_export_dry_run_uses_clip_trim_with_independent_overrides(
    tmp_path: Path, options: tuple[str, ...], name: str
) -> None:
    from conftest import SYNTHETIC_STEP, write_mcraw

    clip = write_mcraw(tmp_path / "a.mcraw", [i * SYNTHETIC_STEP for i in range(120)])
    clip.with_name("a.mcrawsome.toml").write_text('[trim]\nstart = "2"\nduration = "1"\n')
    result = CliRunner().invoke(cli, ["export", str(clip), "--to", "mcraw", "--dry-run", *options])
    assert result.exit_code == 0, result.output
    assert f"-> {tmp_path / name}" in result.output


def test_export_dry_run_uses_trim_from_read_only_folder(tmp_path: Path) -> None:
    from conftest import SYNTHETIC_STEP, write_mcraw

    from mcrawsome.recipe.clip_layer import write_layer

    folder = tmp_path / "read-only"
    folder.mkdir()
    clip = write_mcraw(folder / "a.mcraw", [i * SYNTHETIC_STEP for i in range(120)])
    folder.chmod(0o555)
    try:
        write_layer(clip, {"trim": {"start": "2", "duration": "1"}}, "")
        result = CliRunner().invoke(cli, ["export", str(clip), "--to", "mcraw", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert f"-> {folder / 'a_48-71.mcraw'}" in result.output
    finally:
        folder.chmod(0o755)


def _trimmed_clip(folder: Path, trim: str) -> Path:
    from conftest import SYNTHETIC_STEP, write_mcraw

    clip = write_mcraw(folder / "a.mcraw", [i * SYNTHETIC_STEP for i in range(8)])
    clip.with_name("a.mcrawsome.toml").write_text(
        f'[trim]\n{trim}\n[motion]\nstabilise = "off"\n[develop]\nexposure = 0\n'
    )
    return clip


def test_help_lists_commands_and_generated_options() -> None:
    runner = CliRunner()
    assert "export" in runner.invoke(cli, ["--help"]).output
    help_text = runner.invoke(cli, ["export", "--help"]).output
    assert "--develop.exposure" in help_text
    assert "--output.dng_scale" in help_text


def test_help_hides_layer_three_options_and_help_all_shows_them() -> None:
    runner = CliRunner()
    plain = runner.invoke(cli, ["export", "--help"]).output
    assert "--output.dng_compression" not in plain
    assert "--output.dng_scale" in plain
    everything = runner.invoke(cli, ["export", "--help-all"])
    assert everything.exit_code == 0, everything.output
    line = next(ln for ln in everything.output.splitlines() if "--output.dng_compression" in ln)
    assert "none, lossless" in line
    assert "default none" in line
    assert "--develop.exposure" in everything.output
    assert "--output.dng_compression" not in runner.invoke(cli, ["analyse", "--help"]).output
    assert "--develop.lens_shading" in runner.invoke(cli, ["analyse", "--help-all"]).output


def test_dry_run_shows_the_resolved_recipe(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    result = CliRunner().invoke(cli, ["export", str(clip), "--output.dng_scale", "half", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert 'dng_scale = "half"' in result.output
    assert "[output]" in result.output  # TOML section headers must survive printing


def test_bad_values_fail_before_rendering(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    result = CliRunner().invoke(cli, ["export", str(clip), "--set", "develop.exposre=1"])
    assert result.exit_code != 0
    assert "develop.exposure" in result.output


@pytest.mark.fixture
def test_info_describes_a_clip(fixture_12bit: Path) -> None:
    result = CliRunner().invoke(cli, ["info", str(fixture_12bit)])
    assert result.exit_code == 0, result.output
    assert "3840x2160" in result.output
    assert "pixel 10 pro xl" in result.output.lower()
    assert "frames" in result.output


def test_output_folder_with_trailing_slash_for_one_clip(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    out = tmp_path / "exports"
    out.mkdir()
    result = CliRunner().invoke(cli, ["export", str(clip), "--output", f"{out}/", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert str(out / "a_DNG") in result.output


def test_unsupported_output_suffix_fails_before_rendering(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    result = CliRunner().invoke(
        cli, ["export", str(clip), "--to", "mcraw", "--output", str(tmp_path / "a.gif")]
    )
    assert result.exit_code != 0
    assert ".mcraw" in result.output


def test_batch_with_clashing_names_fails_before_rendering(tmp_path: Path) -> None:
    for folder in ("one", "two"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "a.mcraw").write_bytes(b"x")
    result = CliRunner().invoke(
        cli,
        [
            "export",
            str(tmp_path / "one" / "a.mcraw"),
            str(tmp_path / "two" / "a.mcraw"),
            "--output",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    assert "same output" in result.output


def test_bad_clips_are_reported_and_the_batch_continues(tmp_path: Path) -> None:
    first, second = tmp_path / "first.mcraw", tmp_path / "second.mcraw"
    first.write_bytes(b"not a clip")
    second.write_bytes(b"also not a clip")
    result = CliRunner().invoke(cli, ["export", str(first), str(second), "--output", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "first.mcraw" in result.output
    assert "second.mcraw" in result.output
    assert "Traceback" not in result.output


def test_info_on_a_bad_clip_is_a_clean_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mcraw"
    bad.write_bytes(b"MOTION \x03" + b"\x00" * 64)
    result = CliRunner().invoke(cli, ["info", str(bad)])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "bad.mcraw" in result.output


def _dry(tmp_path: Path, *args: str) -> tuple[int, str]:
    clip = tmp_path / "a.mcraw"
    if not clip.exists():
        clip.write_bytes(b"x")
    result = CliRunner().invoke(cli, ["export", str(clip), "--dry-run", *args])
    return result.exit_code, result.output


@pytest.mark.parametrize(
    ("args", "name"),
    [
        ((), "a_DNG"),
        (("--to", "dng", "--output.dng_scale", "half"), "a_DNG_half"),
        (("--to", "exr"), "a_EXR"),
        (("--to", "mcraw"), "a_repaired.mcraw"),
    ],
)
def test_output_names(tmp_path: Path, args: tuple[str, ...], name: str) -> None:
    code, output = _dry(tmp_path, *args)
    assert code == 0, output
    assert f"-> {tmp_path / name}" in output


@pytest.mark.parametrize(("kind", "suffix"), [("mcraw", ".mp4"), ("dng", ".mov"), ("exr", ".exr")])
def test_wrong_suffix_for_the_output_kind(tmp_path: Path, kind: str, suffix: str) -> None:
    code, output = _dry(tmp_path, "--to", kind, "--output", str(tmp_path / f"x{suffix}"))
    assert code != 0
    assert "x" + suffix in output


@pytest.mark.parametrize("kind", ["av1", "prores", "dnxhr", "hdr-sdr"])
def test_a_video_format_is_refused(tmp_path: Path, kind: str) -> None:
    code, output = _dry(tmp_path, "--set", f"output.kind={kind}")
    assert code != 0
    assert "no longer encodes video" in output


def test_zero_workers_is_a_clean_error(tmp_path: Path) -> None:
    code, output = _dry(tmp_path, "--workers", "0")
    assert code != 0
    assert "Traceback" not in output
    assert "workers" in output.lower()


def test_dry_run_reports_missing_inputs(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["export", str(tmp_path / "gone.mcraw"), "--dry-run"])
    assert result.exit_code != 0
    assert "missing" in result.output


def test_dotted_clip_name_exports_dng(tiny_raw: Path, tmp_path: Path) -> None:
    clip = tmp_path / "my.clip.mcraw"
    clip.symlink_to(tiny_raw)
    result = CliRunner().invoke(cli, ["export", str(clip), "--to", "dng", "--workers", "2"])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "my.clip_DNG").glob("*.dng"))) == 3


@pytest.mark.fixture
def test_info_shows_every_section(fixture_12bit: Path) -> None:
    result = CliRunner().invoke(cli, ["info", str(fixture_12bit)])
    assert result.exit_code == 0, result.output
    for heading in ("Camera", "Lens", "Sensor", "Exposure", "Recording", "MotionCam settings"):
        assert heading in result.output
    assert "Focal length" in result.output


@pytest.mark.fixture
def test_info_raw_dumps_the_metadata(fixture_12bit: Path) -> None:
    import json

    result = CliRunner().invoke(cli, ["info", str(fixture_12bit), "--raw"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "colorMatrix1" in data["container"]
    assert "lensShadingMap" in data["first_frame"]


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from mcrawsome.camera import profile

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    profile.all_profiles.cache_clear()
    yield tmp_path / "config"
    profile.all_profiles.cache_clear()


@pytest.mark.fixture
def test_gyro_reports_the_fit_and_the_plan(fixture_12bit: Path) -> None:
    # three frames cannot confirm a sync, so trust it anyway to see every field
    result = CliRunner().invoke(cli, ["gyro", str(fixture_12bit), "--set", "motion.min_confidence=0"])
    assert result.exit_code == 0, result.output
    for field in (
        "Sync offset", "Axes", "Focal length", "Readout", "Confidence", "Correction", "Reduced", "Coverage",
        "Profile",
    ):  # fmt: skip
        assert field in result.output


@pytest.mark.fixture
def test_gyro_explains_a_sync_it_does_not_trust(fixture_12bit: Path) -> None:
    result = CliRunner().invoke(cli, ["gyro", str(fixture_12bit)])
    assert result.exit_code == 0, result.output
    assert "Confidence" in result.output
    assert "not stabilised" in result.output


@pytest.mark.fixture
def test_gyro_writes_gyroflow_files(fixture_12bit: Path, tmp_path: Path) -> None:
    import json

    from mcrawsome.io.gcsv import read_gcsv

    out = tmp_path / "gyroflow"
    args = ["gyro", str(fixture_12bit), "--set", "motion.min_confidence=0", "--gyroflow", str(out)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    lens = json.loads((out / "clip_12bit_lens.json").read_text())
    assert lens["calib_dimension"] == {"w": 3840, "h": 2160}
    assert read_gcsv(out / "clip_12bit_synced.gcsv").gyro.shape[1] == 3
    again = CliRunner().invoke(cli, args)
    assert again.exit_code != 0
    assert "--force" in again.output


@pytest.mark.fixture
def test_calibrate_without_save_writes_nothing(fixture_12bit: Path, config_home: Path) -> None:
    result = CliRunner().invoke(cli, ["calibrate", str(fixture_12bit)])
    assert result.exit_code == 0, result.output
    assert "focal_px" in result.output
    assert "Confidence" in result.output
    assert not config_home.exists() or not any(config_home.rglob("*.toml"))


@pytest.mark.fixture
def test_calibrate_refuses_to_save_a_poor_calibration(fixture_12bit: Path, config_home: Path) -> None:
    result = CliRunner().invoke(cli, ["calibrate", str(fixture_12bit), "--save"])
    assert result.exit_code != 0
    assert "not saved" in result.output
    assert not config_home.exists() or not any(config_home.rglob("*.toml"))


@pytest.mark.fixture
def test_calibrate_imports_a_gyroflow_profile(fixture_12bit: Path, config_home: Path, tmp_path: Path) -> None:
    import json
    import tomllib

    lens = tmp_path / "lens.json"
    lens.write_text(
        json.dumps(
            {
                "camera_brand": "Google",
                "camera_model": "Pixel 10 Pro XL",
                "calib_dimension": {"w": 4000, "h": 3000},
                "frame_readout_time": 17.0,
                "fisheye_params": {"camera_matrix": [[3100, 0, 2000], [0, 3100, 1500], [0, 0, 1]]},
            }
        )
    )
    args = ["calibrate", str(fixture_12bit), "--from-gyroflow", str(lens), "--name", "my_pixel", "--save"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    saved = tomllib.loads((config_home / "mcrawsome" / "cameras" / "my_pixel.toml").read_text())
    assert saved["focal_px"] == pytest.approx(3100 * 3840 / 4000)
    assert saved["readout_ms"] == 17.0
    assert saved["match"]["width"] == 3840
    assert saved["match"]["device_model"] == "pixel 10 pro xl"
    # the profile it replaces for this camera knew the axes and sync: an import must keep them
    assert saved["gcsv_axes"] == {"axes": [0, 1, 2], "signs": [1, -1, 1]}
    assert saved["gcsv_sync_ms"] == pytest.approx(-105.0)
    assert saved["gpmf_axes"] == {"axes": [2, 0, 1], "signs": [1, -1, 1]}


def _plain_video(path: Path) -> Path:
    import subprocess

    from mcrawsome.output.ffmpeg import find_ffmpeg

    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24",
         "-frames:v", "24", "-c:v", "mpeg4", "-q:v", "2", str(path)], check=True,
    )  # fmt: skip
    return path


def test_a_video_with_an_unknown_encoding_fails_while_planning(tmp_path: Path) -> None:
    video = _plain_video(tmp_path / "holiday.mp4")
    result = CliRunner().invoke(cli, ["export", str(video), "--dry-run"])
    assert result.exit_code != 0
    assert "--develop.input_gamut" in result.output
    assert "--develop.input_encoding" in result.output


def test_highlight_recovery_on_a_video_is_noted(tmp_path: Path) -> None:
    video = _plain_video(tmp_path / "x_Davinci_Davinci-WG.mp4")
    args = ["export", str(video), "--output", str(tmp_path / "out")]
    result = CliRunner().invoke(cli, [*args, "--develop.highlight_recovery", "0.5"])
    assert result.exit_code == 0, result.output
    assert result.output.count("highlight recovery needs RAW input") == 1
    quiet = CliRunner().invoke(cli, [*args, "--force"])
    assert "highlight recovery" not in quiet.output


@pytest.mark.fixture
@pytest.mark.slow
def test_dng_gain_maps_follow_the_lens_shading_setting(fixture_12bit: Path, tmp_path: Path) -> None:
    from mcrawsome.output.dng import parse_gain_maps, read_dng_tags

    def maps(strength: str, folder: str) -> list[np.ndarray]:
        out = tmp_path / folder
        args = ["export", str(fixture_12bit), "--to", "dng", "--output", f"{out}/"]
        result = CliRunner().invoke(cli, [*args, "--develop.lens_shading", strength])
        assert result.exit_code == 0, result.output
        first = min(out.rglob("*.dng"))
        return [m.gains for m in parse_gain_maps(read_dng_tags(first)[51009])]

    for full, half in zip(maps("1", "full"), maps("0.5", "half"), strict=True):
        np.testing.assert_allclose(half, np.sqrt(full), atol=1e-6)


def test_auto_exposure_is_resolved_and_recorded(tiny_raw: Path, tmp_path: Path) -> None:
    import tomllib

    out = tmp_path / f"{tiny_raw.stem}_EXR"  # a folder --output gets the clip's folder inside it
    result = CliRunner().invoke(cli, ["export", str(tiny_raw), "--to", "exr", "--output", f"{tmp_path}/"])
    assert result.exit_code == 0, result.output
    sidecar = tomllib.loads(out.with_name(out.name + ".mcrawsome.toml").read_text())
    assert isinstance(sidecar["recipe"]["develop"]["exposure"], float)
    assert sidecar["measurements"]["exposure_auto"] is True
    assert sidecar["measurements"]["exposure_key"] > 0


def test_a_numeric_exposure_skips_the_measuring_pass(tiny_raw: Path, tmp_path: Path) -> None:
    import tomllib

    out = tmp_path / f"{tiny_raw.stem}_EXR"  # a folder --output gets the clip's folder inside it
    args = ["export", str(tiny_raw), "--to", "exr", "--develop.exposure", "0"]
    result = CliRunner().invoke(cli, [*args, "--output", f"{tmp_path}/"])
    assert result.exit_code == 0, result.output
    sidecar = tomllib.loads(out.with_name(out.name + ".mcrawsome.toml").read_text())
    assert sidecar["recipe"]["develop"]["exposure"] == 0
    assert "exposure_auto" not in sidecar["measurements"]


@pytest.mark.fixture
def test_dng_baseline_exposure_is_the_resolved_auto_value(fixture_12bit: Path, tmp_path: Path) -> None:
    import tomllib

    from mcrawsome.output.dng import read_dng_tags

    out = tmp_path / "dng"
    result = CliRunner().invoke(cli, ["export", str(fixture_12bit), "--to", "dng", "--output", f"{out}/"])
    assert result.exit_code == 0, result.output
    folder = out / "clip_12bit_DNG"
    sidecar = tomllib.loads(folder.with_name(folder.name + ".mcrawsome.toml").read_text())
    exposure = sidecar["recipe"]["develop"]["exposure"]
    assert isinstance(exposure, float)
    tag = read_dng_tags(min(folder.glob("*.dng")))[50730]
    assert float(tag[0] if isinstance(tag, tuple) else tag) == pytest.approx(exposure, abs=1e-3)


@pytest.mark.fixture
@pytest.mark.slow
def test_analyse_reports_every_section_caches_tracks_and_cleans_scratch(
    fixture_12bit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path.with_name(tmp_path.name + "-cache")  # scratch goes outside the checked tree
    monkeypatch.setenv("MCRAWSOME_CACHE_DIR", str(cache))
    clip = tmp_path / "clip.mcraw"
    clip.symlink_to(fixture_12bit)
    (tmp_path / "clip.gcsv").symlink_to(fixture_12bit.with_suffix(".gcsv"))
    before = set(tmp_path.iterdir())
    result = CliRunner().invoke(cli, ["analyse", str(clip)])
    assert result.exit_code == 0, result.output
    for section in ("Exposure", "Clipping", "Noise", "White balance", "Brightness", "Stabilisation"):
        assert section in result.output
    assert set(tmp_path.iterdir()) == before
    files = [path for path in cache.rglob("*") if path.is_file()]
    assert files  # Bounded image-motion measurements are reused by later analysis/sends.
    assert all(path.parent == cache / "motion-tracks" and path.suffix == ".json" for path in files)
    assert not list((cache / "analysis" / "work").iterdir())
    assert (cache / "analysis" / "work").is_dir()  # the scratch folder was used, and is empty again


@pytest.mark.fixture
@pytest.mark.slow
def test_a_trimmed_export_aligns_audio_to_its_first_frame(fixture_12bit: Path, tmp_path: Path) -> None:
    import tomllib
    import wave

    import numpy as np

    from mcrawsome.io.clip import Clip
    from mcrawsome.output.audio import aligned_track
    from mcrawsome.output.ffmpeg import frame_rate

    out = tmp_path / f"{fixture_12bit.stem}_EXR"  # a folder --output gets the clip's folder inside it
    args = ["export", str(fixture_12bit), "--to", "exr", "--start", "1f", "--duration", "1f"]
    result = CliRunner().invoke(cli, [*args, "--output", f"{tmp_path}/"])
    assert result.exit_code == 0, result.output
    sidecar = tomllib.loads(out.with_name(out.name + ".mcrawsome.toml").read_text())
    assert sidecar["measurements"]["frames"] == 1
    assert sidecar["measurements"]["first_frame"] == 1
    with Clip(fixture_12bit) as clip:
        track = clip.audio()
        assert track is not None
        rate = float(frame_rate(clip.fps))
        expected = aligned_track(track, int(clip.timestamps_ns[1]), 1 / rate, rate / clip.fps)
    with wave.open(str(out / f"{fixture_12bit.stem}.wav"), "rb") as sound:
        assert sound.getframerate() == expected.sample_rate
        actual = np.frombuffer(sound.readframes(sound.getnframes()), dtype="<i2")
    np.testing.assert_array_equal(actual.reshape(expected.samples.shape), expected.samples)
    assert sidecar["measurements"]["audio_offset_s"] == 0.0


@pytest.mark.fixture
def test_a_start_past_the_end_fails_before_rendering(fixture_12bit: Path, tmp_path: Path) -> None:
    out = tmp_path / "late"
    result = CliRunner().invoke(cli, ["export", str(fixture_12bit), "--start", "10s", "--output", f"{out}/"])
    assert result.exit_code != 0
    assert "after the end" in result.output
    assert not out.exists()


def test_a_range_past_the_end_is_clamped_with_a_note(tiny_raw: Path, tmp_path: Path) -> None:
    import tomllib

    out = tmp_path / f"{tiny_raw.stem}_EXR"  # a folder --output gets the clip's folder inside it
    args = ["export", str(tiny_raw), "--to", "exr", "--start", "1f", "--duration", "10s"]
    result = CliRunner().invoke(cli, [*args, "--output", f"{tmp_path}/"])
    assert result.exit_code == 0, result.output
    assert "clip ends" in " ".join(result.output.split())
    sidecar = tomllib.loads(out.with_name(out.name + ".mcrawsome.toml").read_text())
    assert sidecar["measurements"]["frames"] == 2


@pytest.mark.fixture
def test_a_trimmed_dng_export_writes_only_its_range(fixture_12bit: Path, tmp_path: Path) -> None:
    out = tmp_path / "dng"
    args = ["export", str(fixture_12bit), "--to", "dng", "--start", "1f", "--duration", "2f"]
    result = CliRunner().invoke(cli, [*args, "--output", f"{out}/"])
    assert result.exit_code == 0, result.output
    names = sorted(p.name for p in (out / "clip_12bit_DNG").glob("*.dng"))
    assert names == ["clip_12bit-000001.dng", "clip_12bit-000002.dng"]


@pytest.mark.fixture
def test_a_lossless_dng_folder_holds_the_same_sensor_values(fixture_12bit: Path, tmp_path: Path) -> None:
    import rawpy

    from mcrawsome.output.dng import read_dng_tags

    def export(folder: str, *extra: str) -> list[Path]:
        out = tmp_path / folder
        args = ["export", str(fixture_12bit), "--to", "dng", "--develop.exposure", "0", *extra]
        result = CliRunner().invoke(cli, [*args, "--output", f"{out}/"])
        assert result.exit_code == 0, result.output
        return sorted((out / "clip_12bit_DNG").glob("*.dng"))

    plain = export("plain")
    small = export("small", "--output.dng_compression", "lossless")
    assert read_dng_tags(small[0])[259] == (7,)
    for a, b in zip(plain, small, strict=True):
        with rawpy.imread(str(a)) as raw_a, rawpy.imread(str(b)) as raw_b:
            np.testing.assert_array_equal(raw_a.raw_image, raw_b.raw_image)
        assert b.stat().st_size < 0.7 * a.stat().st_size


@pytest.mark.fixture
@pytest.mark.slow
def test_a_failed_measuring_pass_fails_that_clip_and_the_batch_goes_on(
    fixture_12bit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis import resolve
    from mcrawsome.analysis.measure import measure_samples as real

    def measure(job: object, frame_count: int, workers: int, progress: object = None) -> object:
        if job.clip_path.name == "a.mcraw":  # type: ignore[attr-defined]
            raise RuntimeError("the clip ends early")
        return real(job, frame_count, workers)  # type: ignore[arg-type]

    monkeypatch.setattr(resolve, "measure_samples", measure)
    for name in ("a", "b"):
        (tmp_path / f"{name}.mcraw").symlink_to(fixture_12bit)
    args = ["export", str(tmp_path / "a.mcraw"), str(tmp_path / "b.mcraw"), "--to", "exr"]
    result = CliRunner().invoke(cli, [*args, "--output", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    flat = " ".join(result.output.split())
    assert "Failed [measure-failed]: a.mcraw" in flat
    assert "Done:" in flat


@pytest.mark.fixture
def test_analyse_measures_brightness_through_the_display(
    fixture_12bit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis import run
    from mcrawsome.pipeline.measure import SdrLight

    seen: list[object] = []

    def measure(job: object, frame_count: int, workers: int) -> SdrLight:
        seen.append(job)
        return SdrLight(0.4, 0.8)

    monkeypatch.setattr(run, "measure_sdr", measure)
    result = CliRunner().invoke(cli, ["analyse", str(fixture_12bit)])
    assert result.exit_code == 0, result.output
    (job,) = seen
    assert job.target == "acescct"  # type: ignore[attr-defined]


@pytest.mark.fixture
def test_a_dng_export_prints_the_auto_exposure_notes(
    fixture_12bit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis import resolve
    from mcrawsome.analysis.measure import SampleStats

    def black(job: object, frame_count: int, workers: int, progress: object = None) -> SampleStats:
        return SampleStats(key=0.0, clipped_share=0.0, noise_rel=0.0, noise_level=0.0, frames=3)

    monkeypatch.setattr(resolve, "measure_samples", black)
    args = ["export", str(fixture_12bit), "--to", "dng", "--output", f"{tmp_path}/"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "too dark to judge" in " ".join(result.output.split())


def _child_processes() -> list[str]:
    """Command lines of this process's live children, except multiprocessing's resource tracker, which
    lives as long as the test process."""
    import os

    children = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            command = (stat.parent / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if int(fields[1]) == os.getpid() and fields[0] != "Z" and "resource_tracker" not in command:
            children.append(command)
    return children


def test_an_existing_sidecar_alone_is_refused_unless_forced(tmp_path: Path) -> None:
    import json

    from mcrawsome.output.plan import plan_export

    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    (tmp_path / "a_DNG.mcrawsome.toml").write_text("")
    result = CliRunner().invoke(cli, ["export", str(clip), "--json"])
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "output-exists"
    assert "a_DNG.mcrawsome.toml already exists" in error["message"]
    args: dict[str, Any] = {"to": None, "stabilise": None, "crop": None, "start": None, "duration": None}
    args |= {"fields": {}, "sets": (), "output": None, "dry_run": False, "space_check": False}
    [planned] = plan_export([clip], [], force=True, **args)
    assert planned.target == tmp_path / "a_DNG"


@pytest.mark.parametrize(
    ("args", "note"),
    [
        (("--to", "exr", "--output.dng_transfer", "log"), "output.dng_transfer: CinemaDNG only"),
        (("--to", "mcraw", "--output.frame_rate", "integer"), "output.frame_rate has no effect on MCRAW"),
    ],
)
def test_settings_that_do_nothing_are_noted_at_planning(
    tmp_path: Path, args: tuple[str, ...], note: str
) -> None:
    code, output = _dry(tmp_path, *args)
    assert code == 0, output
    assert output.count(note) == 1, output


def _spy_budget(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    from mcrawsome.cli import processing as main
    from mcrawsome.pipeline.render import worker_budget as real

    sizes: list[tuple[int, int]] = []

    def spy(job: Any, size: tuple[int, int], requested: int, available: int | None) -> tuple[int, str | None]:
        sizes.append(size)
        return real(job, size, requested, available)

    monkeypatch.setattr(main, "worker_budget", spy)
    return sizes
