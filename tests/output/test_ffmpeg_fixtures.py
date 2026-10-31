"""Shared video fixtures must also work with the distribution's LGPL build."""

import sys
from pathlib import Path

import pytest
from conftest import generate_direct_log_video, generate_rotated_video, generate_synthetic_video

from mcrawsome.io.directlog import probe_direct_log
from mcrawsome.output.ffmpeg import find_ffmpeg


@pytest.fixture(autouse=True)
def _without_gpl_encoders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ffmpeg, ffprobe = find_ffmpeg()
    folder = tmp_path / "lgpl-bin"
    folder.mkdir()
    wrapper = folder / "ffmpeg"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if any(arg in ('libx264', 'libx265') for arg in sys.argv):\n"
        "    print('GPL encoder requested by test fixture', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        f"os.execv({str(ffmpeg)!r}, [{str(ffmpeg)!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    (folder / "ffprobe").symlink_to(ffprobe)
    monkeypatch.setenv("MCRAWSOME_FFMPEG", str(wrapper))


@pytest.fixture
def synthetic_video(tmp_path: Path) -> Path:
    return generate_synthetic_video(tmp_path)


@pytest.fixture(params=[0, 90, 180, 270])
def rotated_video(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, int]:
    return generate_rotated_video(tmp_path, int(request.param))


@pytest.fixture(params=["av1", "hevc", "prores"])
def direct_log_video(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    return generate_direct_log_video(tmp_path, str(request.param))


def test_synthetic_fixture_without_gpl(synthetic_video: Path) -> None:
    assert probe_direct_log(find_ffmpeg()[1], synthetic_video).frame_count == 30


def test_rotation_fixture_without_gpl(rotated_video: tuple[Path, int]) -> None:
    video, rotation = rotated_video
    assert probe_direct_log(find_ffmpeg()[1], video).rotation == -rotation % 360


def test_direct_log_fixture_without_gpl(direct_log_video: Path) -> None:
    info = probe_direct_log(find_ffmpeg()[1], direct_log_video)
    assert info.codec == direct_log_video.name.split("_")[0]
    assert info.frame_count == 3
    assert info.bit_depth == 10
