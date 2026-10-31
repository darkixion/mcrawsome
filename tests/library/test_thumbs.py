"""Library thumbnails stay neutral regardless of saved Develop settings."""

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from mcrawsome.develop.raw import Developer
from mcrawsome.errors import McError
from mcrawsome.library import thumbs
from mcrawsome.library.scan import clip_id
from mcrawsome.paths import cache_dir
from mcrawsome.preview.session import PreviewSession

STEP = 41_666_667
CUBE = "LUT_3D_SIZE 2\n" + "0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n"


def _decode(jpeg: bytes) -> np.ndarray:
    assert jpeg[:2] == b"\xff\xd8"
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return np.asarray(image)


@pytest.fixture
def developed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts RAW frames developed for previews."""
    calls: list[int] = []
    real = Developer.develop_half

    def counting(self: Developer, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Developer, "develop_half", counting)
    return calls


@pytest.fixture
def raw(tmp_path: Path, make_mcraw: Callable[..., Path]) -> Path:
    folder = tmp_path / "clips"
    folder.mkdir()
    return make_mcraw(folder / "a.mcraw", [i * STEP for i in range(5)])


def _cached() -> list[str]:
    return sorted(path.name for path in (cache_dir() / "thumbs").glob("*.jpg"))


@pytest.mark.fixture
def test_a_raw_thumbnail_is_320_wide_from_the_middle_frame(
    fixture_10bit_rotated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept: list[int] = []
    real = PreviewSession.keep

    def keep(self: PreviewSession, index: int) -> None:
        kept.append(index)
        real(self, index)

    monkeypatch.setattr(PreviewSession, "keep", keep)
    image = _decode(thumbs.thumbnail(fixture_10bit_rotated, clip_id(fixture_10bit_rotated)))
    assert image.shape == (180, 320, 3)
    assert kept == [1]  # the middle of three frames


def test_a_video_thumbnail_is_its_decoded_frame_at_320_wide(synthetic_video: Path) -> None:
    image = _decode(thumbs.thumbnail(synthetic_video, clip_id(synthetic_video)))
    assert image.shape == (180, 320, 3)


def test_a_thumbnail_is_made_once_then_read_from_the_cache(raw: Path, developed: list[int]) -> None:
    first = thumbs.thumbnail(raw, clip_id(raw))
    assert thumbs.thumbnail(raw, clip_id(raw)) == first
    assert len(developed) == 1
    [name] = _cached()
    assert name.startswith(f"{clip_id(raw)}-")


def test_changing_develop_settings_does_not_change_the_library_thumbnail(
    raw: Path, developed: list[int]
) -> None:
    before = thumbs.thumbnail(raw, clip_id(raw))
    layer = raw.with_name("a.mcrawsome.toml")
    content = 'look = "bond"\n[develop]\nexposure = 3\n[grade]\nsat = 0.5\n'
    layer.write_text(content)
    after = thumbs.thumbnail(raw, clip_id(raw))
    assert len(developed) == 1
    assert after == before
    assert len(_cached()) == 1
    assert layer.read_text() == content


def test_saved_orientation_changes_neutral_thumbnail_and_cache(raw: Path, developed: list[int]) -> None:
    before = _decode(thumbs.thumbnail(raw, clip_id(raw)))
    raw.with_suffix(".mcrawsome.toml").write_text("[develop]\nrotation = 90\nexposure = 3\n")
    after = _decode(thumbs.thumbnail(raw, clip_id(raw)))
    assert after.shape == (before.shape[1], before.shape[0], 3)
    assert np.abs(after.astype(float) - np.rot90(before, -1)).mean() < 3
    assert len(developed) == 2
    assert len(_cached()) == 2


def test_first_thumbnail_matches_neutral_camera_view(raw: Path) -> None:
    from mcrawsome.preview.session import PreviewSettings
    from mcrawsome.recipe.load import build_recipe

    recipe = build_recipe([{"look": "neutral", "develop": {"exposure": 0}, "motion": {"stabilise": "off"}}])
    with PreviewSession(raw, recipe, PreviewSettings(width=thumbs.WIDTH)) as session:
        expected = session.frame(session.frame_count // 2)
    assert thumbs.thumbnail(raw, clip_id(raw)) == expected


def test_the_cache_cap_removes_the_oldest_thumbnails_first(raw: Path) -> None:
    folder = cache_dir() / "thumbs"
    folder.mkdir(parents=True)
    now = time.time()
    for age, name in ((300, "oldest"), (200, "older"), (100, "newer")):
        path = folder / f"{name}.jpg"
        path.write_bytes(b"x" * 4000)
        os.utime(path, (now - age, now - age))
    made = thumbs.thumbnail(raw, clip_id(raw), cap_bytes=4000 + 2000)
    assert len(made) < 2000
    assert _cached() == sorted(["newer.jpg", next(n for n in _cached() if n.startswith(clip_id(raw)))])


def test_a_clip_removed_before_it_is_opened_is_missing(raw: Path) -> None:
    identity = clip_id(raw)
    moved = shutil.move(raw, raw.with_name("elsewhere.bin"))
    assert moved
    with pytest.raises(McError) as missing:
        thumbs.thumbnail(raw, identity)
    assert missing.value.code == "clip-missing"


def test_camera_settings_change_the_thumbnail_fingerprint() -> None:
    from mcrawsome.recipe.load import build_recipe

    before = build_recipe([])
    after = build_recipe([{"camera": {"readout_ms": 10}}])
    assert thumbs.fingerprint(before) != thumbs.fingerprint(after)


def test_remosaic_preference_changes_the_thumbnail_fingerprint() -> None:
    from mcrawsome.recipe.load import build_recipe
    from mcrawsome.server.settings import InterfaceSettings, save_settings

    recipe = build_recipe([])
    before = thumbs.fingerprint(recipe)
    save_settings(InterfaceSettings(quad_bayer_remosaic=False))
    assert thumbs.fingerprint(recipe) != before


def test_a_raw_thumbnail_shows_the_steadiest_frame_by_the_gyro_log(
    tmp_path: Path,
    make_mcraw: Callable[..., Path],
    make_gcsv: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.analysis import motion_insights

    source = make_mcraw(tmp_path / "steady.mcraw", [i * STEP for i in range(96)])
    shown: list[int] = []
    real = PreviewSession.frame

    def frame(self: PreviewSession, index: int) -> bytes:
        shown.append(index)
        return real(self, index)

    monkeypatch.setattr(PreviewSession, "frame", frame)
    thumbs.thumbnail(source, clip_id(source))
    assert shown == [48]  # no gyro log: the middle frame
    make_gcsv(source.with_suffix(".gcsv"), 96, shake_deg_s=10)
    steadiest = motion_insights.load(source, every_frame=False)
    assert steadiest is not None
    expected = steadiest.steadiest()
    assert expected is not None
    assert 24 <= expected < 72
    thumbs.thumbnail(source, clip_id(source))  # a new log is a new thumbnail
    assert shown == [48, expected]


def test_a_card_skims_frames_spread_through_the_clip_made_once(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skimming a card shows SKIM_FRAMES pictures from across the clip, each made as its thumbnail is."""
    clip = make_mcraw(tmp_path / "long.mcraw", [i * STEP for i in range(40)])
    shown: list[int] = []
    real = PreviewSession.frame

    def frame(self: PreviewSession, index: int) -> bytes:
        shown.append(index)
        return real(self, index)

    monkeypatch.setattr(PreviewSession, "frame", frame)
    frames = thumbs.skim(clip, clip_id(clip))
    assert len(frames) == thumbs.SKIM_FRAMES == 10
    assert shown == [2, 6, 10, 14, 18, 22, 26, 30, 34, 38]  # the middle of each tenth
    assert {_decode(jpeg).shape for jpeg in frames} == {_decode(thumbs.thumbnail(clip, clip_id(clip))).shape}
    assert thumbs.skim(clip, clip_id(clip)) == frames
    assert len(shown) == 11  # the skim made once, besides the thumbnail's own frame


def test_a_short_clip_repeats_frames_to_fill_its_skim(raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[int] = []
    real = PreviewSession.frame

    def frame(self: PreviewSession, index: int) -> bytes:
        shown.append(index)
        return real(self, index)

    monkeypatch.setattr(PreviewSession, "frame", frame)
    frames = thumbs.skim(raw, clip_id(raw))  # five frames
    assert len(frames) == 10
    assert shown == [0, 1, 2, 3, 4]  # each frame made once
    assert frames[0] == frames[1] != frames[2]


def test_a_skim_follows_the_saved_orientation(raw: Path) -> None:
    before = _decode(thumbs.skim(raw, clip_id(raw))[0])
    raw.with_suffix(".mcrawsome.toml").write_text("[develop]\nrotation = 90\n")
    after = _decode(thumbs.skim(raw, clip_id(raw))[0])
    assert after.shape == (before.shape[1], before.shape[0], 3)
    assert len(list((cache_dir() / "skims").iterdir())) == 2
