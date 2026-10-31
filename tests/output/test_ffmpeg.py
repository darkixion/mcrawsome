from fractions import Fraction
from pathlib import Path

import pytest

from mcrawsome.output import ffmpeg as ff


def test_frame_rate_snaps_to_standard_rates() -> None:
    assert ff.frame_rate(24.0029) == Fraction(24)
    assert ff.frame_rate(23.98) == Fraction(24000, 1001)
    assert ff.frame_rate(17.3) == Fraction(173, 10)


def test_missing_ffmpeg_is_reported_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCRAWSOME_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ff.FfmpegError, match="FFmpeg"):
        ff.find_ffmpeg()


def _fake_ffmpeg(tmp_path: Path, encoders: str, decoders: str) -> None:
    for name, text in (
        ("ffmpeg", f"if [ \"$2\" = -encoders ]; then echo '{encoders}'; else echo '{decoders}'; fi"),
        ("ffprobe", "true"),
    ):
        script = tmp_path / name
        script.write_text(f"#!/bin/sh\n{text}\n")
        script.chmod(0o755)


def test_path_pair_can_live_in_different_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCRAWSOME_FFMPEG", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: f"/tools/{name}/bin/{name}")
    encoder, probe = ff.find_ffmpeg()
    assert ff.ffprobe_for(encoder) == probe
