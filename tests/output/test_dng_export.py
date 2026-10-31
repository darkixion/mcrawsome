import struct
import tomllib
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mcrawsome.errors import McError
from mcrawsome.output.chunks import WorkFolder
from mcrawsome.output.dng import read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe

RECIPE = build_recipe([{"output": {"kind": "dng"}}])


@pytest.mark.parametrize("rotation", [90, 180, 270])
@pytest.mark.parametrize("scale", ["full", "half"])
def test_saved_orientation_exports_without_turning_the_raw_mosaic(
    tiny_raw: Path, tmp_path: Path, rotation: int, scale: str
) -> None:
    recipe = build_recipe(
        [
            {
                "develop": {"rotation": rotation},
                "output": {"kind": "dng", "dng_scale": scale, "dng_compression": "none"},
            }
        ]
    )
    result = export_dng(tiny_raw, tmp_path / "rotated_DNG", recipe, workers=1)
    tags = read_dng_tags(next(result.output.glob("*.dng")))
    assert tags[274] == ({90: 6, 180: 3, 270: 8}[rotation],)
    assert (tags[256][0], tags[257][0]) == ((128, 64) if scale == "full" else (64, 32))


@pytest.mark.fixture
@pytest.mark.slow
def test_a_stopped_dng_export_resumes_with_the_frames_it_has(
    fixture_10bit_rotated: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "clip_DNG"
    asked: list[bool] = []

    def after_two() -> bool:
        asked.append(True)
        return len(asked) == 2

    with pytest.raises(McError) as caught:
        export_dng(fixture_10bit_rotated, folder, RECIPE, workers=1, stop=after_two)
    assert caught.value.code == "paused"
    assert not folder.exists()
    staged = WorkFolder(folder).publish_path
    names = [f"clip_10bit_rotated-{i:06d}.dng" for i in range(3)]
    kept = {name: (staged / name).stat().st_mtime_ns for name in names[:2]}
    # a frame in flight when the export stopped may be on disk, perhaps cut short: either way the
    # resumed export must write it again
    third = staged / names[2]
    third.write_bytes(third.read_bytes()[:5000] if third.exists() else b"II*\0")

    result = export_dng(fixture_10bit_rotated, folder, RECIPE, workers=1)
    assert {name: (folder / name).stat().st_mtime_ns for name in names[:2]} == kept
    assert "resumed: 2 of 3 frames were already written" in result.warnings
    assert not WorkFolder(folder).path.exists()

    whole = tmp_path / "whole_DNG"
    export_dng(fixture_10bit_rotated, whole, RECIPE, workers=1)
    assert sorted(p.name for p in folder.iterdir()) == sorted(p.name for p in whole.iterdir())
    for path in whole.iterdir():
        assert (folder / path.name).read_bytes() == path.read_bytes(), path.name


def test_a_thermal_limit_without_a_sensor_is_noted(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcrawsome.output.export.find_cpu_sensor", lambda: None)
    result = export_dng(tiny_raw, tmp_path / "clip_DNG", RECIPE, workers=1, thermal_limit=85.0)
    assert "no CPU temperature sensor found; --thermal-limit has no effect" in result.warnings
    plain = export_dng(tiny_raw, tmp_path / "plain_DNG", RECIPE, workers=1)
    assert not any("thermal" in note or "CPU" in note for note in plain.warnings)


@pytest.mark.fixture
@pytest.mark.slow
def test_a_recovered_clip_exports_to_dng_with_the_recovery_note(
    fixture_10bit_rotated: Path, tmp_path: Path
) -> None:
    cut = tmp_path / "cut.mcraw"
    data = fixture_10bit_rotated.read_bytes()
    (offset,) = struct.unpack("<q", data[-8:])
    cut.write_bytes(data[: offset - 8])
    result = export_dng(cut, tmp_path / "cut_DNG", RECIPE, workers=1)
    assert "Recovered: the recording was not finalised; 3 frames were found by scanning" in result.warnings


def test_dng_export_from_relative_paths(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("clip.mcraw").symlink_to(tiny_raw)
    result = export_dng(Path("clip.mcraw"), Path("out_DNG"), RECIPE, workers=1)
    assert result.output == tmp_path / "out_DNG"
    assert result.frames == 3
    assert len(list(result.output.glob("*.dng"))) == 3
    assert result.sidecar.exists()


@pytest.mark.fixture
@pytest.mark.slow
def test_a_failed_dng_sidecar_keeps_the_work_for_a_second_run(
    fixture_12bit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.output import dng_export
    from mcrawsome.output.sidecar import sidecar_path

    folder = tmp_path / "out_DNG"

    def broken(*_args: object, **_kwargs: object) -> Path:
        raise OSError("sidecar disk trouble")

    with monkeypatch.context() as patch:
        patch.setattr(dng_export, "write_sidecar", broken)
        with pytest.raises(McError, match="sidecar disk trouble") as caught:
            export_dng(fixture_12bit, folder, RECIPE, workers=1)
    assert caught.value.code == "verify-failed"
    work = WorkFolder(folder)
    assert not folder.exists()
    assert not sidecar_path(folder).exists()
    kept = {p.name: p.stat().st_mtime_ns for p in work.publish_path.glob("*.dng")}
    assert len(kept) == 3
    result = export_dng(fixture_12bit, folder, RECIPE, workers=1)
    assert {p.name: p.stat().st_mtime_ns for p in folder.glob("*.dng")} == kept
    assert result.sidecar.exists()
    assert not work.path.exists()


@pytest.mark.fixture
@pytest.mark.slow
def test_a_rerun_finishes_a_dng_publish_that_stopped_before_the_sidecar(
    fixture_12bit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.output.sidecar import sidecar_path

    folder = tmp_path / "out_DNG"
    with monkeypatch.context() as patch:
        patch.setattr(WorkFolder, "discard", lambda self: None)
        export_dng(fixture_12bit, folder, RECIPE, workers=1)
    work = WorkFolder(folder)
    sidecar_path(folder).replace(sidecar_path(work.publish_path))
    result = export_dng(fixture_12bit, folder, RECIPE, workers=1)
    assert "finished publishing the interrupted export" in result.warnings
    assert result.sidecar.exists()
    assert result.frames == 3
    assert not work.path.exists()


@pytest.mark.fixture
@pytest.mark.slow
def test_a_plain_existing_dng_folder_is_still_refused(fixture_12bit: Path, tmp_path: Path) -> None:
    folder = tmp_path / "out_DNG"
    export_dng(fixture_12bit, folder, RECIPE, workers=1)
    with pytest.raises(McError) as caught:
        export_dng(fixture_12bit, folder, RECIPE, workers=1)
    assert caught.value.code == "output-exists"


def _synthetic_dng(
    tmp_path: Path, make_mcraw: Callable[..., Path], **clip: Any
) -> tuple[Path, dict[str, Any]]:
    timestamps = clip.pop("timestamps", [i * 41_666_667 for i in range(6)])
    path = make_mcraw(tmp_path / "s.mcraw", timestamps, **clip)
    result = export_dng(path, tmp_path / "s_DNG", RECIPE, workers=2)
    return result.output, tomllib.loads(result.sidecar.read_text())


def test_every_dng_frame_carries_the_recipe_white_balance(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    neutrals = [(0.5 + 0.02 * i, 1.0, 0.6 + 0.03 * i) for i in range(6)]
    folder, sidecar = _synthetic_dng(tmp_path, make_mcraw, neutrals=neutrals)
    found = {read_dng_tags(path)[50728] for path in sorted(folder.glob("*.dng"))}
    assert len(found) == 1
    np.testing.assert_allclose(found.pop(), sidecar["measurements"]["white_balance_neutral"], rtol=1e-6)


def test_custom_white_balance_is_constant_in_exported_dngs(white_balance_raw: Path, tmp_path: Path) -> None:
    from mcrawsome.colour.white_balance import colour_info, neutral_for_temperature
    from mcrawsome.io.clip import Clip

    recipe = build_recipe(
        [
            {
                "develop": {"white_balance": "custom", "temperature": 6000, "tint": 20},
                "output": {"kind": "dng", "dng_compression": "none"},
            }
        ]
    )
    with Clip(white_balance_raw) as clip:
        neutral = neutral_for_temperature(colour_info(clip.metadata, clip.frame_metadata(0)), 6000, 20)
    result = export_dng(white_balance_raw, tmp_path / "custom_DNG", recipe, workers=1)
    paths = sorted(result.output.glob("*.dng"))
    assert len(paths) == 3
    for path in paths:
        assert read_dng_tags(path)[50728] == pytest.approx(neutral, abs=1e-6)


def test_the_dng_wav_is_tempo_corrected_for_the_labelled_rate(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    measured = 24.04  # the phone's clock: labelled 24 fps
    timestamps = [round(i * 1e9 / measured) for i in range(30)]
    audio = np.zeros((round(1.4 * 48000), 2), np.int16)
    audio[40000] = 20000  # a click 0.833 s after the first frame
    folder, sidecar = _synthetic_dng(tmp_path, make_mcraw, timestamps=timestamps, audio=audio)
    with wave.open(str(folder / "s.wav")) as wav:
        rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), "<i2").reshape(-1, 2)
    assert sidecar["measurements"]["frames"] == 30
    assert abs(len(samples) - 30 / 24 * rate) <= 1
    tempo = 24 / measured  # as the video mux's atempo: the sound plays at the labelled rate's speed
    assert abs(int(np.argmax(samples[:, 0])) - 40000 / tempo) <= 2


@pytest.mark.fixture
@pytest.mark.parametrize("compression", ["none", "lossless"])
def test_cli_exports_half_dng_proxies(fixture_10bit_rotated: Path, tmp_path: Path, compression: str) -> None:
    import tomllib

    import numpy as np
    import rawpy
    from click.testing import CliRunner

    from mcrawsome.cli.main import cli
    from mcrawsome.io.clip import Clip
    from mcrawsome.output.dng import half_cfa, read_dng_tags
    from mcrawsome.output.dng_export import _frame_problem

    result = CliRunner().invoke(
        cli,
        [
            "export",
            str(fixture_10bit_rotated),
            "--to",
            "dng",
            "--output.dng_scale",
            "half",
            "--output.dng_compression",
            compression,
            "--develop.exposure",
            "0",
            "--workers",
            "2",
            "--output",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    folder = tmp_path / "clip_10bit_rotated_DNG_half"
    files = sorted(folder.glob("*.dng"))
    assert len(files) == 3
    with Clip(fixture_10bit_rotated) as clip:
        for i, path in enumerate(files):
            assert _frame_problem(path, (1920, 1080)) is None
            tags = read_dng_tags(path)
            assert tags[259] == (7 if compression == "lossless" else 1,)
            assert tags[274] == (3,)
            with rawpy.imread(str(path)) as raw:
                np.testing.assert_array_equal(raw.raw_image, half_cfa(clip.raw_frame(i).data, "rggb"))
    side = tomllib.loads(folder.with_name(folder.name + ".mcrawsome.toml").read_text())
    assert side["recipe"]["output"]["dng_scale"] == "half"
    assert side["measurements"]["frames"] == 3
    assert (folder / "clip_10bit_rotated.wav").exists()
    assert not WorkFolder(folder).path.exists()


@pytest.mark.fixture
@pytest.mark.slow
@pytest.mark.parametrize("compression", ["none", "lossless"])
def test_a_stopped_half_dng_export_resumes(
    fixture_10bit_rotated: Path, tmp_path: Path, compression: str
) -> None:
    from mcrawsome.output.dng_export import _frame_problem

    recipe = build_recipe([{"output": {"kind": "dng", "dng_scale": "half", "dng_compression": compression}}])
    folder = tmp_path / "clip_DNG_half"
    with pytest.raises(McError) as caught:
        export_dng(fixture_10bit_rotated, folder, recipe, workers=1, stop=lambda: True)
    assert caught.value.code == "paused"
    staged = WorkFolder(folder).publish_path
    first = staged / "clip_10bit_rotated-000000.dng"
    kept = first.stat().st_mtime_ns
    count = sum(_frame_problem(p, (1920, 1080)) is None for p in staged.glob("*.dng"))
    result = export_dng(fixture_10bit_rotated, folder, recipe, workers=1)
    assert (folder / first.name).stat().st_mtime_ns == kept
    assert f"resumed: {count} of 3 frames were already written" in result.warnings


def test_time_of_day_timecode_is_in_every_frame_and_the_wav(tiny_raw: Path, tmp_path: Path) -> None:
    from mcrawsome.io.clip import Clip
    from mcrawsome.io.timecode import clip_timecode

    recipe = build_recipe([{"output": {"kind": "dng", "timecode": "time-of-day", "timecode_zone": "UTC"}}])
    result = export_dng(tiny_raw, tmp_path / "timed_DNG", recipe, workers=1)
    with Clip(tiny_raw, metadata_only=True) as clip:
        code = clip_timecode(clip, "UTC", float(clip.fps))
    frames = sorted(result.output.glob("*.dng"))
    assert [bytes(read_dng_tags(path)[51043]) for path in frames] == [
        code.address(n) for n in range(len(frames))
    ]
    assert any("time-of-day timecode from " + code.text() in note for note in result.warnings)
    wav = next(result.output.glob("*.wav")).read_bytes()
    assert wav[12:16] == b"bext"
    assert struct.unpack_from("<Q", wav, 12 + 8 + 338)[0] == code.time_reference(48000)
    plain = export_dng(tiny_raw, tmp_path / "plain_DNG", RECIPE, workers=1)
    assert 51043 not in read_dng_tags(next(plain.output.glob("*.dng")))
