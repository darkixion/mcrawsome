import json
import tomllib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerError
from mcrawsome.io.describe import dropped_frames, frame_map
from mcrawsome.output.dng_export import export_dng
from mcrawsome.output.ffmpeg import frame_rate
from mcrawsome.pipeline.units import Unit, plan_units
from mcrawsome.recipe.load import build_recipe

STEP = 41_666_667  # ns between frames at 24 fps
WIDTH, HEIGHT = 128, 64


GAPPED = [i * STEP for i in (0, 1, 2, 4, 5, 6)]  # frame 3 was dropped
PLAIN = build_recipe([])


@pytest.fixture
def gapped(tmp_path: Path, make_mcraw: Callable[..., Path]) -> Path:
    # sound for all seven frame slots of the recording, as a real recording has, so its length shows
    # whether the picture kept pace with it
    audio = np.zeros((round(7 * STEP / 1e9 * 48000), 2), np.int16)
    return make_mcraw(tmp_path / "gapped.mcraw", GAPPED, audio=audio)


def _recorded_sound_s(clip_path: Path) -> float:
    """The length of the recording's sound, retimed to the labelled rate (as the export plays it)."""
    with Clip(clip_path) as clip:
        track = clip.audio()
        assert track is not None
        seconds = len(track.samples) / track.sample_rate
        return seconds * clip.fps / float(frame_rate(clip.fps))


def test_frame_map_without_gaps_is_every_frame() -> None:
    timestamps = np.arange(5, dtype=np.int64) * STEP
    assert frame_map(timestamps, STEP) == list(range(5))


def test_a_gap_of_two_intervals_repeats_the_frame_before_once() -> None:
    timestamps = np.array([0, 1, 2, 4, 5], dtype=np.int64) * STEP
    assert frame_map(timestamps, STEP) == [0, 1, 2, 2, 3, 4]
    assert len(frame_map(timestamps, STEP)) - len(timestamps) == dropped_frames(timestamps)


def test_a_gap_of_three_intervals_repeats_it_twice() -> None:
    timestamps = np.array([0, 1, 4, 5], dtype=np.int64) * STEP
    assert frame_map(timestamps, STEP) == [0, 1, 1, 1, 2, 3]
    assert len(frame_map(timestamps, STEP)) - len(timestamps) == dropped_frames(timestamps)


def test_jitter_under_one_and_a_half_intervals_repeats_nothing() -> None:
    timestamps = (np.array([0.0, 1.0, 2.4, 3.0, 4.45, 5.0]) * STEP).astype(np.int64)
    assert frame_map(timestamps, STEP) == list(range(6))
    assert dropped_frames(timestamps) == 0


def test_units_count_output_frames_and_take_source_neighbours() -> None:
    sources = [0, 1, 2, 2, 3, 4]
    assert plan_units(6, radius=1, step=1, sources=sources) == [
        Unit((0, 1), 0, 1, 0),
        Unit((0, 1, 2), 1, 1, 1),
        Unit((1, 2, 3), 1, 2, 2),  # made once, shown twice
        Unit((2, 3, 4), 1, 1, 4),
        Unit((3, 4), 1, 1, 5),
    ]
    assert plan_units(6, radius=1, step=1, span=(3, 2), sources=sources) == [
        Unit((1, 2, 3), 1, 1, 3),
        Unit((2, 3, 4), 1, 1, 4),
    ]
    assert plan_units(6, radius=0, step=1, indices=[2, 3], sources=sources) == [
        Unit((2,), 0, 1, 2),
        Unit((2,), 0, 1, 3),
    ]
    assert plan_units(6, radius=0, step=2, sources=sources) == [
        Unit((0, 1), None, 2, 0),
        Unit((2,), None, 2, 2),
        Unit((3, 4), None, 2, 4),
    ]


def test_fill_dropped_off_keeps_the_recorded_frames(gapped: Path, tmp_path: Path) -> None:
    output = tmp_path / "off"
    args = ["export", str(gapped), "--to", "dng", "--output", str(output), "--json"]
    result = CliRunner().invoke(cli, [*args, "--output.fill_dropped", "false", "--workers", "2"])
    assert result.exit_code == 0, result.output
    exported = Path(json.loads(result.output)["results"][0]["output"])
    assert len(list(exported.glob("*.dng"))) == len(GAPPED)
    assert not any("dropped" in note for note in json.loads(result.output)["results"][0]["notes"])


def test_a_dng_export_writes_the_repeat_again(gapped: Path, tmp_path: Path) -> None:
    recipe = build_recipe([{"output": {"kind": "dng"}}])
    result = export_dng(gapped, tmp_path / "out_DNG", recipe, workers=2)
    names = sorted(p.name for p in result.output.glob("*.dng"))
    assert names == [f"gapped-{i:06d}.dng" for i in range(7)]
    assert result.frames == 7
    assert "filled 1 dropped frame (at 0.1 s) so sound stays in sync" in result.warnings
    sidecar = tomllib.loads(result.sidecar.read_text())
    assert sidecar["frames"] == {"source_count": 6, "output_count": 7, "repeated": [3]}
    with Clip(gapped) as clip:
        assert clip.frame_count == 6


APPROVED_LOCAL = [
    "251011_153704",
    "261004_174022",
    "261004_174122",
    "261006_005913",
    "261008_095010",
    "261010_131928",
    "261010_135132",
    "261010_154156",
]


@pytest.mark.local
def test_the_local_clips_have_no_gaps(local_dir: Path) -> None:
    checked, skipped = [], []
    for stem in APPROVED_LOCAL:
        matches = sorted(local_dir.glob(f"{stem}*.mcraw"))
        if not matches:
            skipped.append(f"{stem}: missing")
            continue
        path = matches[0]
        try:
            clip = Clip(path)
        except ContainerError as error:
            skipped.append(f"{stem}: {error}")
            continue
        with clip:
            timestamps = clip.timestamps_ns
            assert frame_map(timestamps, 1e9 / clip.fps) == list(range(clip.frame_count)), path.name
        checked.append(stem)
    print(f"checked {checked}; skipped {skipped}")
    if not checked and len(skipped) == len(APPROVED_LOCAL) and all("missing" in x for x in skipped):
        pytest.skip(f"no approved local clips in {local_dir}")
    assert checked, skipped
