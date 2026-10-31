import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli


def run(*args: str | Path) -> tuple[int, str]:
    result = CliRunner().invoke(cli, [str(a) for a in args])
    return result.exit_code, result.stdout


def test_missing_clip_prints_its_code(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["export", str(tmp_path / "gone.mcraw"), "--output", str(tmp_path) + "/"]
    )
    assert result.exit_code == 1
    assert "Failed [input-missing]" in result.output


def test_missing_clip_as_json(tmp_path: Path) -> None:
    code, out = run("export", tmp_path / "gone.mcraw", "--output", f"{tmp_path}/", "--json")
    assert code == 1
    document = json.loads(out)
    assert document["results"][0]["error"]["code"] == "input-missing"
    assert document["results"][0]["clip"].endswith("gone.mcraw")


def test_dry_run_json_gives_output_and_recipe(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    code, out = run("export", clip, "--output.dng_scale", "half", "--dry-run", "--json")
    assert code == 0
    planned = json.loads(out)["results"][0]
    assert planned["output"].endswith("a_DNG_half")
    assert planned["recipe"]["output"]["dng_scale"] == "half"


def test_planning_error_as_json(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    code, out = run("export", clip, "--set", "develop.exposre=1", "--json")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "recipe-invalid"
    assert "develop.exposure" in error["message"]


@pytest.mark.fixture
def test_info_json_has_the_frame_count(fixture_10bit_rotated: Path) -> None:
    code, out = run("info", fixture_10bit_rotated, "--json")
    assert code == 0
    assert json.loads(out)["frames"] == 3


def test_analyse_json_has_the_report_sections(tiny_raw: Path) -> None:
    code, out = run("analyse", tiny_raw, "--json", "--workers", "2")
    assert code == 0
    assert out.startswith("{"), "nothing but the document on stdout"
    document = json.loads(out)
    titles = {"Exposure", "Clipping", "Noise", "White balance", "Brightness", "Stabilisation"}
    assert titles | {"clip", "warnings", "suggestions"} == set(document)
    assert isinstance(document["Brightness"], dict)
    assert not any(ch in out for ch in "━╸╺")


def test_export_json_has_no_progress_output(tiny_raw: Path, tmp_path: Path) -> None:
    code, out = run(
        "export", tiny_raw, "--output", f"{tmp_path}/", "--workers", "2", "--json",
    )  # fmt: skip
    assert code == 0, out
    assert out.startswith("{")
    assert not any(ch in out for ch in "━╸╺")
    result = json.loads(out)["results"][0]
    assert result["error"] is None
    assert result["frames"] == 3
    assert Path(result["output"]).exists()


def test_dry_run_json_marks_missing_clips_and_exits_one(tmp_path: Path) -> None:
    here = tmp_path / "a.mcraw"
    here.write_bytes(b"x")
    code, out = run(
        "export", here, tmp_path / "gone.mcraw", "--output", f"{tmp_path}/", "--dry-run", "--json"
    )
    assert code == 1
    assert [r["missing"] for r in json.loads(out)["results"]] == [False, True]


@pytest.mark.fixture
def test_bad_start_is_recipe_invalid(fixture_10bit_rotated: Path, tmp_path: Path) -> None:
    code, out = run("export", fixture_10bit_rotated, "--output", f"{tmp_path}/", "--start", "soon", "--json")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "recipe-invalid"


def test_an_os_error_in_one_export_keeps_the_batch_and_the_one_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    from mcrawsome.cli import processing as main_module
    from mcrawsome.errors import McError
    from mcrawsome.output.export import ExportResult

    clips = [tmp_path / f"{name}.mcraw" for name in "abc"]
    for clip in clips:
        clip.write_bytes(b"x")

    def export(clip: Path, target: Path, recipe: object, **_: object) -> ExportResult:
        if clip.name == "a.mcraw":
            raise OSError(errno.EIO, "Input/output error", str(clip))
        if clip.name == "b.mcraw":
            raise McError(f"another mcrawsome process (pid 1) is exporting to {target}", code="output-exists")
        target.write_bytes(b"made")
        return ExportResult(target, target.with_suffix(".toml"), 3, None)

    monkeypatch.setattr(main_module, "export_dng", export)
    code, out = run("export", *clips, "--output", f"{tmp_path}/", "--json")
    assert code == 1
    results = json.loads(out)["results"]
    assert [r["error"]["code"] if r["error"] else None for r in results] == [
        "input-unreadable", "output-exists", None,
    ]  # fmt: skip
