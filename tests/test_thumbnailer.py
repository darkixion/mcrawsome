"""A file manager's thumbnail of a recording: the first frame as playback drafts it, neutral, at most SIZE
pixels across, as a PNG; made without the user's folders, settings or GPU, which GNOME's sandbox lacks."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
import pytest

from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.schema import DevelopParams, MotionParams, Recipe
from mcrawsome.thumbnailer import main, thumbnail

STEP = 41_666_667


def test_a_thumbnail_is_the_first_frame_as_playback_drafts_it(tiny_raw: Path) -> None:
    bgr = thumbnail(tiny_raw, 64)
    assert bgr.dtype == np.uint8
    assert bgr.shape == (32, 64, 3)
    recipe = Recipe(develop=DevelopParams(exposure=0), motion=MotionParams(stabilise="off"))
    settings = PreviewSettings(gpu=False, quad_bayer_remosaic=True, cache_mib=0, fast_open=True)
    with PreviewSession(tiny_raw, recipe, settings) as session:
        drafted = cv2.imdecode(np.frombuffer(session.fast(0, 64).jpeg, np.uint8), cv2.IMREAD_COLOR)
        with pytest.raises(IndexError):
            session.still(session.frame_count, 64)
    assert drafted is not None
    assert drafted.shape == bgr.shape
    # The same draft, less the JPEG's loss: small per pixel on a tiny, colourful frame, and gone on average.
    error = drafted.astype(np.int16) - bgr
    assert np.abs(error).mean() < 6
    assert np.abs(error.mean(axis=(0, 1))).max() < 1.5


def test_a_portrait_frame_fits_the_size_too(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "portrait.mcraw", [0, STEP], size=(64, 128))
    assert thumbnail(clip, 48).shape == (48, 24, 3)


def test_the_file_manager_gets_a_png_of_the_size_it_asked_for(tiny_raw: Path, tmp_path: Path) -> None:
    output = tmp_path / "thumbnail.png"
    assert main(["32", str(tiny_raw), str(output)]) == 0
    assert output.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    image = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
    assert image is not None
    assert image.shape == (16, 32, 3)


@pytest.mark.parametrize("arguments", [[], ["large", "a.mcraw", "b.png"], ["128", "a.mcraw"]])
def test_wrong_arguments_explain_the_usage(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(arguments) == 2
    assert "mcrawsome.thumbnailer SIZE INPUT OUTPUT" in capsys.readouterr().err


def test_an_unreadable_recording_fails_without_a_thumbnail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.mcraw"
    broken.write_bytes(b"not a recording")
    output = tmp_path / "thumbnail.png"
    assert main(["128", str(broken), str(output)]) == 1
    assert not output.exists()
    assert "broken.mcraw: no thumbnail" in capsys.readouterr().err


def test_it_needs_nothing_but_the_recording(tiny_raw: Path, tmp_path: Path) -> None:
    """As GNOME runs it: the recording alone in a read-only folder, a home folder that is not there, no
    settings, and nothing written but the thumbnail."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    clip = sandbox / "gnome-desktop-file-to-thumbnail.mcraw"
    clip.write_bytes(tiny_raw.read_bytes())
    output = tmp_path / "out" / "thumbnail.png"
    output.parent.mkdir()
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "MCRAWSOME_"))}
    environment["HOME"] = str(tmp_path / "absent")
    before = set(tmp_path.rglob("*"))
    sandbox.chmod(0o555)
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "mcrawsome.thumbnailer", "128", str(clip), str(output)],
            env=environment,
            cwd="/",
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        sandbox.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert set(tmp_path.rglob("*")) == before | {output}
