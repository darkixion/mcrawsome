import itertools
import subprocess
from pathlib import Path

import pytest

from mcrawsome import __version__
from mcrawsome.output import chunks
from mcrawsome.output.chunks import Encoder, InputFile, Manifest, WorkFolder
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.recipe.load import build_recipe


def _manifest(clip: Path, **changes: object) -> Manifest:
    recipe = build_recipe([])
    fields: dict[str, object] = {
        "mcrawsome_version": __version__,
        "input": InputFile.of(clip),
        "requested": recipe,
        "resolved": recipe,
        "measurements": {"frames": 30},
        "frame_range": None,
        "chunk_frames": 240,
        "encoder": Encoder(name="svt-av1", codec="av1", args=["-crf", "10"]),
        "light": None,
        "plan_file": None,
    }
    return Manifest.model_validate(fields | changes)


def test_manifest_round_trip_and_matching(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x" * 1000)
    work = WorkFolder(tmp_path / "a.mp4")
    assert work.load() is None
    stored = _manifest(clip, light=(812, 140), plan_file="stabilisation.npz")
    work.path.mkdir()
    work.save(stored)
    assert work.load() == stored
    # what was measured does not decide a match; what was asked for does
    assert work.matches(_manifest(clip, measurements={}, light=None))
    assert not work.matches(_manifest(clip, requested=build_recipe([{"develop": {"exposure": 1}}])))
    assert not work.matches(_manifest(clip, frame_range=(0, 10)))
    assert not work.matches(_manifest(clip, chunk_frames=10))
    assert not work.matches(
        _manifest(clip, encoder=Encoder(name="svt-av1", codec="av1", args=["-crf", "11"]))
    )
    assert not work.matches(_manifest(clip, mcrawsome_version="0.0.1"))
    clip.write_bytes(b"y" * 1000)
    assert not work.matches(_manifest(clip))
    (work.path / "manifest.json").write_text("{not json")
    assert work.load() is None


def _frames(path: Path, pix_fmt: str) -> bytes:
    return subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"],
        capture_output=True,
        check=True,
    ).stdout


def _pts(path: Path) -> list[float]:
    run = subprocess.run(
        [str(find_ffmpeg()[1]), "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return sorted(float(line) for line in run.stdout.split())


def _assert_continuous(path: Path) -> None:
    pts = _pts(path)
    assert len(pts) == 30
    steps = [b - a for a, b in itertools.pairwise(pts)]
    assert all(abs(step - 1 / 24) < 0.002 for step in steps), steps


def test_a_work_folder_without_a_manifest_starts_again_with_a_note(tmp_path: Path) -> None:
    work = WorkFolder(tmp_path / "a.mp4")
    assert chunks.resume_or_restart(work, False, lambda m: m) == (None, [])  # no interrupted export
    (work.path / "stabilisation.npz").write_bytes(b"x")  # stopped while measuring or planning
    assert chunks.resume_or_restart(work, False, lambda m: m) == (None, [chunks.NOTHING_KEPT])
    assert list(work.path.iterdir()) == []
    assert chunks.resume_or_restart(work, True, lambda m: m) == (None, [])  # --restart says nothing


def test_relative_work_paths_keep_a_symlinked_folder_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "clip.mcraw").write_bytes(b"input")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    work = WorkFolder(Path("alias/out.mp4"))
    assert work.path == alias / ".out.mp4.mcrawsome-work"
    assert InputFile.of(Path("alias/clip.mcraw")).path == str(real / "clip.mcraw")
    assert InputFile.of(Path("alias/clip.mcraw")) == InputFile.of(Path("real/../real/clip.mcraw").absolute())
