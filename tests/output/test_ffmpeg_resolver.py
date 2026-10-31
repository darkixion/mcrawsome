import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcrawsome import doctor, support
from mcrawsome.io.gpmf import extract_gpmf
from mcrawsome.output.ffmpeg import FfmpegError, ffprobe_for, find_ffmpeg


@pytest.fixture
def private_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    calls = tmp_path / "calls"
    for folder in (tmp_path / "old", tmp_path / "private pair"):
        folder.mkdir()
        version = "7.1.5" if folder.name == "old" else "8.1.3"
        for name in ("ffmpeg", "ffprobe"):
            script = folder / name
            script.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                f"with open({str(calls)!r}, 'a') as log: log.write(sys.argv[0] + '\\n')\n"
                "if '-version' in sys.argv:\n"
                f"    print('{name} version {version}\\nconfiguration: --disable-gpl --disable-nonfree')\n"
                "elif '-encoders' in sys.argv: print('libsvtav1 libopus prores_ks libvpx-vp9 dnxhd')\n"
                "elif '-decoders' in sys.argv: print('libdav1d\\n V....D apv APV decoder')\n"
                "else: print(json.dumps({'streams': []}))\n"
            )
            script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "old"))
    ffmpeg, ffprobe = (tmp_path / "private pair" / name for name in ("ffmpeg", "ffprobe"))
    monkeypatch.setenv("MCRAWSOME_FFMPEG", str(ffmpeg))
    return ffmpeg, ffprobe, calls


def test_override_wins_over_path(private_pair: tuple[Path, Path, Path]) -> None:
    ffmpeg, ffprobe, calls = private_pair
    assert find_ffmpeg() == (ffmpeg, ffprobe)
    assert not calls.exists()


def test_missing_private_sibling_names_both_paths(private_pair: tuple[Path, Path, Path]) -> None:
    ffmpeg, ffprobe, _ = private_pair
    ffprobe.unlink()
    with pytest.raises(FfmpegError) as error:
        find_ffmpeg()
    assert str(ffmpeg) in str(error.value)
    assert str(ffprobe) in str(error.value)


def test_spawned_interpreter_inherits_override(private_pair: tuple[Path, Path, Path]) -> None:
    ffmpeg, ffprobe, _ = private_pair
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            'from mcrawsome.output.ffmpeg import find_ffmpeg; print(*find_ffmpeg(), sep="\\n")',
        ],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    assert run.stdout.splitlines() == [str(ffmpeg), str(ffprobe)]


def test_doctor_and_support_report_private_pair(
    private_pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, ffprobe, calls = private_pair
    checks = {check.name: check for check in doctor._ffmpeg_checks(False)}
    assert str(ffmpeg) in checks["FFmpeg"].detail
    assert str(ffprobe) in checks["ffprobe"].detail
    assert "8.1" in checks["FFmpeg"].detail
    assert "AV1 decoder" in checks
    info = support.system_info()
    assert info["ffmpeg_path"] == str(ffmpeg)
    assert info["ffprobe_path"] == str(ffprobe)
    assert "8.1" in info["ffmpeg"]
    assert set(calls.read_text().splitlines()) <= {str(ffmpeg), str(ffprobe)}


def test_gpmf_uses_resolved_probe(private_pair: tuple[Path, Path, Path], tmp_path: Path) -> None:
    ffmpeg, _, calls = private_pair
    old = ffmpeg.parent.parent / "old"
    assert extract_gpmf(old / "ffmpeg", tmp_path / "video.mov") is None
    # The argument is honoured: only its sibling ffprobe runs, and the resolver does not run again.
    assert calls.read_text().splitlines() == [str(old / "ffprobe")]


def test_unset_override_keeps_path_behaviour(
    private_pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, _, _ = private_pair
    monkeypatch.delenv("MCRAWSOME_FFMPEG")
    old = ffmpeg.parent.parent / "old"
    assert find_ffmpeg() == (old / "ffmpeg", old / "ffprobe")


def test_empty_override_counts_as_unset(
    private_pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_FFMPEG", "")
    old = private_pair[0].parent.parent / "old"
    assert find_ffmpeg() == (old / "ffmpeg", old / "ffprobe")


def test_doctor_fix_names_the_bad_override(
    private_pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    private_pair[1].unlink()
    failed = [c for c in doctor._ffmpeg_checks(False) if c.name == "FFmpeg"]
    fix = failed[0].fix or ""
    assert "MCRAWSOME_FFMPEG" in fix
    assert "unset" in fix
    assert "Install FFmpeg" not in fix
    monkeypatch.delenv("MCRAWSOME_FFMPEG")
    monkeypatch.setenv("PATH", "")
    failed = [c for c in doctor._ffmpeg_checks(False) if c.name == "FFmpeg"]
    assert (failed[0].fix or "").startswith("Install FFmpeg")


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
def test_override_requires_executable_files(private_pair: tuple[Path, Path, Path], name: str) -> None:
    ffmpeg, ffprobe, _ = private_pair
    (ffmpeg.parent / name).chmod(0o644)
    with pytest.raises(FfmpegError) as error:
        find_ffmpeg()
    assert str(ffmpeg) in str(error.value)
    assert str(ffprobe) in str(error.value)


def test_export_metadata_uses_resolved_probe(
    private_pair: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io.directlog import DirectLogError
    from mcrawsome.output import export
    from mcrawsome.recipe.schema import Recipe

    ffmpeg, _, calls = private_pair
    old = ffmpeg.parent.parent / "old"
    video = tmp_path / "video.mov"
    video.touch()
    observed: list[Path] = []

    def probe(path: Path, video: Path) -> None:
        observed.append(path)
        raise DirectLogError("test stops after discovery")

    monkeypatch.setattr(export, "probe_direct_log", probe)
    with pytest.raises(export.ExportError, match="test stops after discovery"):
        export.read_video_facts(old / "ffmpeg", video, Recipe())
    assert observed == [ffprobe_for(old / "ffmpeg")] == [old / "ffprobe"]
    assert not calls.exists()


def test_discovery_is_confined_to_resolver() -> None:
    import ast

    root = Path(__file__).resolve().parents[2] / "src/mcrawsome"
    for path in root.rglob("*.py"):
        if path == root / "output/ffmpeg.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("which", "with_name"):
                continue
            assert not any(
                isinstance(arg, ast.Constant) and arg.value in ("ffmpeg", "ffprobe") for arg in node.args
            ), path


@pytest.mark.parametrize(
    "module",
    [
        "mcrawsome.pipeline.render",
        "mcrawsome.pipeline.calibrate",
        "mcrawsome.output.plan",
        "mcrawsome.preview.session",
        "mcrawsome.output.export",
        "mcrawsome.output.space",
        "mcrawsome.cli.processing",
        "mcrawsome.io.describe",
        "mcrawsome.io.directlog",
        "mcrawsome.doctor",
        "mcrawsome.support",
    ],
)
def test_each_discovery_site_uses_private_pair(module: str, private_pair: tuple[Path, Path, Path]) -> None:
    import importlib

    ffmpeg, ffprobe, calls = private_pair
    site = importlib.import_module(module)
    assert site.find_ffmpeg() == (ffmpeg, ffprobe)
    assert not calls.exists()
