"""OpenEXR image sequences in scene-linear ACEScg or Linear Rec.709 (Milestone 7 part B, task 6)."""

import dataclasses
import errno
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import OpenEXR
import pytest

from mcrawsome.colour.spaces import ACES_WHITE, AP1, AP1_TO_REC709
from mcrawsome.colour.transfer import acescct_decode
from mcrawsome.errors import McError
from mcrawsome.output.export import clip_job
from mcrawsome.output.exr_export import export_exr
from mcrawsome.output.plan import output_name
from mcrawsome.output.sidecar import sidecar_path
from mcrawsome.pipeline.render import render_frame
from mcrawsome.recipe.load import RecipeError, build_recipe
from mcrawsome.recipe.schema import Recipe

_TIMES = [0, 41_666_667, 83_333_333]


def _recipe(**output: str) -> Recipe:
    return build_recipe([{"output": {"kind": "exr", **output}}])


def _pixels(path: Path) -> tuple[dict[str, Any], npt.NDArray[np.float32]]:
    with OpenEXR.File(str(path)) as exr:
        return dict(exr.header()), np.asarray(exr.channels()["RGB"].pixels, dtype=np.float32)


@pytest.fixture
def clip(tmp_path: Path, make_mcraw: Callable[..., Path]) -> Path:
    return make_mcraw(tmp_path / "clip.mcraw", _TIMES)


def test_exr_recipes_and_names() -> None:
    recipe = _recipe()
    assert (recipe.output.exr_space, recipe.output.exr_compression) == ("acescg", "zip")
    assert output_name(Path("clip.mcraw"), recipe) == "clip_EXR"
    # Legacy creative settings are discarded; saved motion settings remain readable.
    saved = build_recipe(
        [{"output": {"kind": "exr", "log": "acescct"}, "hdr": True, "motion": {"stabilise": "on"}}]
    )
    assert saved.output == recipe.output
    assert saved.motion.stabilise == "on"
    with pytest.raises(RecipeError, match="exr_compression"):
        _recipe(exr_compression="invalid")


def test_an_acescg_sequence(clip: Path, tmp_path: Path) -> None:
    folder = tmp_path / "clip_EXR"
    result = export_exr(clip, folder, _recipe(), workers=2)
    assert result.output == folder
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["clip.wav", "clip_000000.exr", "clip_000001.exr", "clip_000002.exr"]
    header, pixels = _pixels(folder / "clip_000001.exr")
    expected_chroma = (*AP1[0], *AP1[1], *AP1[2], *ACES_WHITE)
    np.testing.assert_allclose(header["chromaticities"], expected_chroma, atol=1e-7)
    side = tomllib.loads(sidecar_path(folder).read_text(encoding="utf-8"))
    measured = side["measurements"]
    assert measured["exr_space"] == "acescg"
    assert "scene linear" in measured["note"]
    # the same frame as the ACEScct render, before its 16-bit log quantisation
    job, _, _ = clip_job(clip, _recipe())
    exposure = float(side["recipe"]["develop"]["exposure"])
    aces = dataclasses.replace(job, target="acescct", float_output=False, exposure_stops=exposure)
    reference = acescct_decode(render_frame(aces, 1).astype(np.float64) / 65535)
    np.testing.assert_allclose(pixels, reference, rtol=2e-3, atol=2e-4)


def test_linear_rec709_is_acescg_through_the_matrix(clip: Path, tmp_path: Path) -> None:
    export_exr(clip, tmp_path / "aces", _recipe(), workers=2)
    export_exr(
        clip, tmp_path / "rec709", _recipe(exr_space="linear-rec709", exr_compression="piz"), workers=2
    )
    _, aces = _pixels(tmp_path / "aces" / "clip_000000.exr")
    header, rec709 = _pixels(tmp_path / "rec709" / "clip_000000.exr")
    np.testing.assert_allclose(rec709, aces @ AP1_TO_REC709.T, rtol=3e-3, atol=3e-4)
    assert header["compression"] == OpenEXR.PIZ_COMPRESSION


def test_a_stopped_sequence_resumes(clip: Path, tmp_path: Path) -> None:
    folder = tmp_path / "clip_EXR"
    with pytest.raises(McError) as caught:
        export_exr(clip, folder, _recipe(), workers=2, stop=lambda: True)
    assert caught.value.code == "paused"
    assert not folder.exists()
    result = export_exr(clip, folder, _recipe(), workers=2)
    assert "resumed: 1 of 3 frames were already written" in result.warnings
    assert len(list(folder.glob("*.exr"))) == 3


def test_an_existing_folder_needs_force(clip: Path, tmp_path: Path) -> None:
    folder = tmp_path / "clip_EXR"
    export_exr(clip, folder, _recipe(), workers=2)
    with pytest.raises(McError) as caught:
        export_exr(clip, folder, _recipe(), workers=2)
    assert caught.value.code == "output-exists"
    export_exr(clip, folder, _recipe(), workers=2, force=True)


def test_a_trimmed_direct_log_video(synthetic_video: Path, tmp_path: Path) -> None:
    folder = tmp_path / "video_EXR"
    result = export_exr(synthetic_video, folder, _recipe(), workers=2, frame_range=(4, 2))
    assert result.frames == 2
    stem = synthetic_video.stem
    assert sorted(p.name for p in folder.glob("*.exr")) == [f"{stem}_000004.exr", f"{stem}_000005.exr"]
    _, pixels = _pixels(folder / f"{stem}_000004.exr")
    assert pixels.shape == (180, 320, 3)


def test_the_cli_exports_exr(clip: Path, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from mcrawsome.cli.main import cli

    result = CliRunner().invoke(
        cli, ["export", str(clip), "--to", "exr", "--output", f"{tmp_path}/", "--workers", "2"]
    )
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "clip_EXR").glob("*.exr"))) == 3


def test_brackets_in_a_clip_name_are_literal(clip: Path, tmp_path: Path) -> None:
    named = clip.with_name("clip_[take1].mcraw")
    clip.rename(named)
    folder = tmp_path / "bracketed_EXR"
    result = export_exr(named, folder, _recipe(), workers=2)
    assert result.frames == 3
    assert sorted(p.name for p in folder.iterdir() if p.suffix == ".exr") == [
        f"clip_[take1]_{index:06d}.exr" for index in range(3)
    ]


def test_a_full_disk_at_the_sidecar_is_resumable(
    clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.output import exr_export
    from mcrawsome.output.chunks import WorkFolder

    def full(*args: Any, **kwargs: Any) -> Path:
        raise OSError(errno.ENOSPC, "No space left on device")

    folder = tmp_path / "full_EXR"
    with monkeypatch.context() as patch:
        patch.setattr(exr_export, "write_sidecar", full)
        with pytest.raises(McError) as caught:
            export_exr(clip, folder, _recipe(), workers=2)
    assert caught.value.code == "disk-full"
    assert not folder.exists()
    kept = WorkFolder(folder).publish_path / "clip_000001.exr"
    before = kept.stat().st_mtime_ns
    result = export_exr(clip, folder, _recipe(), workers=2)
    assert "resumed: 3 of 3 frames were already written" in result.warnings
    assert (folder / kept.name).stat().st_mtime_ns == before


def test_time_of_day_timecode_gives_each_frame_its_own_address(clip: Path, tmp_path: Path) -> None:
    from mcrawsome.io.clip import Clip
    from mcrawsome.io.timecode import clip_timecode

    result = export_exr(
        clip, tmp_path / "clip_EXR", _recipe(timecode="time-of-day", timecode_zone="UTC"), workers=2
    )
    with Clip(clip, metadata_only=True) as recording:
        code = clip_timecode(recording, "UTC", float(recording.fps))
    for number, path in enumerate(sorted(result.output.glob("*.exr"))):
        header, _ = _pixels(path)
        hours, minutes, seconds, frames, _ = code.frame(number)
        stamp = header["timeCode"]
        assert (stamp.hours, stamp.minutes, stamp.seconds, stamp.frame) == (hours, minutes, seconds, frames)
        assert header["framesPerSecond"] > 0
    header, _ = _pixels(
        next(export_exr(clip, tmp_path / "plain_EXR", _recipe(), workers=2).output.glob("*.exr"))
    )
    assert "timeCode" not in header
