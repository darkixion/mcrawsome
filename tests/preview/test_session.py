"""PreviewSession: a kept preview-size frame, re-finished on each setting change."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from mcrawsome.colour.spaces import D65, REC709, REC2020, conversion
from mcrawsome.colour.transfer import pq_encode
from mcrawsome.develop.raw import Developer
from mcrawsome.io.clip import Clip
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.paths import cache_dir
from mcrawsome.pipeline.stabilise import MotionOutcome, StabiliseError
from mcrawsome.preview import session as session_module
from mcrawsome.preview.session import PreviewSession, PreviewSettings
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.schema import Recipe

STEP = 41_666_667  # ns between frames at 24 fps


def _decode(jpeg: bytes) -> np.ndarray:
    assert jpeg[:2] == b"\xff\xd8"
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return np.asarray(image)


def _recipe(**layer: Any) -> Recipe:
    return build_recipe([layer])


def _clip(make_mcraw: Callable[..., Path], tmp_path: Path) -> Path:
    folder = tmp_path / "clips"
    folder.mkdir()
    return make_mcraw(folder / "clip.mcraw", [i * STEP for i in range(4)])


@pytest.mark.fixture
def test_a_raw_frame_is_at_most_1920_wide_with_the_clips_aspect(fixture_10bit_rotated: Path) -> None:
    with PreviewSession(fixture_10bit_rotated, _recipe(develop={"exposure": 0.0})) as session:
        image = _decode(session.frame(1))
    assert image.shape == (1080, 1920, 3)


def test_a_small_raw_clip_is_shown_at_half_size(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe(develop={"exposure": 0.0})) as session:
        image = _decode(session.frame(0))
    assert image.shape == (32, 64, 3)  # 128 x 64 photosites, one pixel per quad


def test_a_narrower_preview_width_is_kept(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    settings = PreviewSettings(width=32)
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe(), settings) as session:
        image = _decode(session.frame(0))
    assert image.shape == (16, 32, 3)


def test_a_direct_log_frame_keeps_the_videos_size(synthetic_video: Path) -> None:
    with PreviewSession(synthetic_video, _recipe(develop={"exposure": 0.0})) as session:
        image = _decode(session.frame(3))
    assert image.shape == (180, 320, 3)


def test_a_rotated_video_is_shown_upright(rotated_video: tuple[Path, int]) -> None:
    video, degrees = rotated_video
    with PreviewSession(video, _recipe(develop={"exposure": 0.0})) as session:
        image = _decode(session.frame(0))
    assert image.shape[:2] == ((160, 96) if degrees % 180 else (96, 160))


def test_a_wide_video_is_scaled_to_the_preview_width(synthetic_video: Path) -> None:
    with PreviewSession(synthetic_video, _recipe(), PreviewSettings(width=160)) as session:
        image = _decode(session.frame(0))
    assert image.shape == (90, 160, 3)


class _Spy:
    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn, self.calls = fn, 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.fn(*args, **kwargs)


def test_a_develop_change_develops_again(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    develop = _Spy(Developer.develop_half)
    monkeypatch.setattr(Developer, "develop_half", lambda self, raw: develop(self, raw))
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe(develop={"exposure": 0.0})) as session:
        session.frame(2)
        session.frame(2)  # the kept frame
        assert develop.calls == 1
        session.update(_recipe(develop={"exposure": 0.0, "lens_shading": 0.0}))
        assert develop.calls == 2
        session.update(_recipe(develop={"exposure": 0.0, "lens_shading": 0.0}, camera={"readout_ms": 9.0}))
        assert develop.calls == 3
        session.frame(3)
        assert develop.calls == 4


def _pq_grey(nits: float) -> np.ndarray:
    return np.full((1, 3), float(pq_encode(nits)))


def _pq_from_bt709(rgb: tuple[float, float, float], times: float = 1.0) -> np.ndarray:
    """PQ BT.2020 code values for a BT.709 colour given relative to SDR white (1.0 = 203 nits)."""
    to_2020 = conversion(REC709, D65, REC2020, D65)
    return np.asarray(pq_encode(times * np.asarray([rgb]) @ to_2020.T * 203.0))


def _shaky_plan(staging: Path) -> Path:
    """Every frame turned about the vertical axis: a horizontal shift of 60 pixels at 3840 wide."""
    angle = np.arctan(60 / 3000)
    c, s = np.cos(angle), np.sin(angle)
    turn = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)
    rotations = np.broadcast_to(turn, (3, 8, 3, 3)).copy()
    plan = StabilisationPlan(rotations, np.tile([1.0, 0, 0, 0], (3, 1)), 3000.0, 1.0, 3840, 2160, 0, 0, 0, 1)
    path = staging / "stabilisation.npz"
    plan.save(path)
    return path


@pytest.mark.fixture
@pytest.mark.slow
def test_refined_frames_are_stabilised(fixture_12bit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []

    def plan(clip: Path, recipe: Recipe, info: object, ffmpeg: Path, workers: int, staging: Path,
             cache: Path | None = None) -> tuple[MotionOutcome, dict[str, str]]:  # fmt: skip
        calls.append(staging)
        return MotionOutcome(_shaky_plan(staging)), {}

    monkeypatch.setattr(session_module, "plan_motion", plan)
    with PreviewSession(fixture_12bit, _recipe(develop={"exposure": 0.0})) as session:
        draft = _decode(session.frame(1))
        refined = _decode(session.refined(1).jpeg)
        session.refined(1)
        staging = calls[0]
        assert staging.is_relative_to(cache_dir() / "preview")
    assert len(calls) == 1  # planned once per session
    assert not staging.exists()  # removed on close
    assert refined.shape == draft.shape == (1080, 1920, 3)
    grey = [cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) for image in (draft, refined)]
    (dx, dy), _ = cv2.phaseCorrelate(grey[0], grey[1])
    assert abs(abs(dx) - 30) < 3
    assert abs(dy) < 2


def test_a_failed_motion_plan_can_be_planned_again(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    """Stabilisation asked for without a gyro log fails; with it back on auto, refined frames work."""
    recipe = _recipe(develop={"exposure": 0.0})
    with PreviewSession(_clip(make_mcraw, tmp_path), recipe) as session:
        draft = _decode(session.frame(1))
        session.update(_recipe(develop={"exposure": 0.0}, motion={"stabilise": "on"}))
        for _ in range(2):  # the same Recipe again plans again, and fails the same way
            with pytest.raises(StabiliseError, match="no gyro log"):
                session.refined(1)
        session.update(recipe)
        assert _decode(session.refined(1).jpeg).shape == draft.shape
    assert list((cache_dir() / "preview").iterdir()) == []


def _files(folder: Path) -> dict[str, float]:
    return {str(p.relative_to(folder)): p.stat().st_mtime for p in folder.rglob("*")}


def test_nothing_is_written_beside_the_clip(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    clip = _clip(make_mcraw, tmp_path)
    before = _files(clip.parent)
    with PreviewSession(clip, _recipe(develop={"exposure": 0.0})) as session:
        session.frame(1)
        session.update(_recipe(look="gritty", develop={"exposure": 1.0}))
        session.refined(1)
    assert _files(clip.parent) == before
    assert not cache_dir().exists() or all(
        p.is_relative_to(cache_dir() / "preview") for p in cache_dir().rglob("*") if p.is_file()
    )


def test_refined_frames_carry_the_motion_steps_notes(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def plan(clip: Path, recipe: Recipe, info: object, ffmpeg: Path, workers: int, staging: Path,
             cache: Path | None = None) -> tuple[MotionOutcome, dict[str, str]]:  # fmt: skip
        return MotionOutcome(None, notes=["not stabilised: the clip has no gyro log"]), {}

    monkeypatch.setattr(session_module, "plan_motion", plan)
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe(develop={"exposure": 0.0})) as session:
        refined = session.refined(1)
    assert refined.notes == ["not stabilised: the clip has no gyro log"]
    assert _decode(refined.jpeg).shape == (32, 64, 3)


def test_an_automatic_exposure_is_measured_as_an_export_measures_it(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    from mcrawsome.analysis.resolve import resolve_auto
    from mcrawsome.output.export import clip_job

    clip = _clip(make_mcraw, tmp_path)
    recipe = _recipe(develop={"exposure": "auto"})
    job, frames, _ = clip_job(clip, recipe)
    expected = resolve_auto(recipe, job, frames, 1).recipe.develop.exposure
    assert expected != 0.0
    with PreviewSession(clip, recipe) as session:
        before = session.frame(0)
        assert session.auto_pending  # shown at 0 stops until measured
        measurer = session.auto_measurer()
        assert measurer is not None
        key, measure = measurer
        session.set_auto(key, measure())
        assert not session.auto_pending
        assert session.auto_measurer() is None
        assert session.refinish() != before
        exposed = session.update(_recipe(develop={"exposure": expected}))
        assert not session.auto_pending
        assert session.update(recipe) == exposed  # the measured value is kept for these settings
    assert session.auto_stops(recipe) == expected


def test_a_set_exposure_needs_no_measuring(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe(develop={"exposure": 1.0})) as session:
        assert not session.auto_pending
        assert session.auto_measurer() is None


def test_auto_key_is_public_and_follows_the_recipe(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    with PreviewSession(_clip(make_mcraw, tmp_path), build_recipe([])) as session:
        key = session.auto_key()
        assert key is not None
        assert session.auto_key(build_recipe([])) == key
        assert session.auto_key(build_recipe([{"develop": {"exposure": 0.0}}])) is None


def test_drafts_use_the_raw_overrides_and_crop(make_mcraw: Callable[..., Path], tmp_path: Path) -> None:
    from mcrawsome.camera.colour import with_colour_matrix
    from mcrawsome.develop.crop import CropBox, crop_raw

    path = _clip(make_mcraw, tmp_path)
    custom = (0.9, -0.3, -0.1, -0.4, 1.3, 0.1, -0.05, 0.2, 0.6)
    recipe = _recipe(
        develop={"crop": "64x32", "black_level": 0, "white_level": 800, "lens_shading_mode": "colour"},
        camera={"colour_matrix": custom},
    )
    with PreviewSession(path, recipe) as session, Clip(path) as clip:
        raw = crop_raw(clip.raw_frame(0), CropBox(0, 0, 64, 32))
        expected = Developer(
            with_colour_matrix(clip.metadata, custom),
            raw.metadata,
            session._job.neutral,
            shading_mode="colour",
            black_level=0,
            white_level=800,
        ).develop_half(raw.data)
        session.keep(0)
        assert session._kept is not None
        np.testing.assert_allclose(session._kept[1], expected, rtol=1e-6)


def test_changing_the_preview_rate_reloads_the_timeline(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe()) as session:
        session.keep(3)
        session.set_recipe(_recipe(output={"frame_rate": 12}, develop={"crop": "64x32"}))
        assert session.frame_count == 2
        assert session.rate == 12
        assert session.size == (32, 16)
        session.set_recipe(_recipe(output={"frame_rate": 24}, develop={"crop": "64x32"}))
        assert session.frame_count == 4
        assert session.rate == 24


def test_first_jpeg_does_not_read_audio(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_audio(self: Clip) -> None:
        raise AssertionError("audio must not be read before the first JPEG")

    monkeypatch.setattr(Clip, "audio", unavailable_audio)
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe()) as session:
        assert session.frame(0).startswith(b"\xff\xd8")


def test_video_first_jpeg_does_not_extract_audio(
    synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_audio(*args: Any) -> None:
        raise AssertionError("audio must not be extracted before the first JPEG")

    monkeypatch.setattr("mcrawsome.output.export.read_audio", unavailable_audio)
    with PreviewSession(synthetic_video, _recipe()) as session:
        assert session.frame(0).startswith(b"\xff\xd8")


def test_opening_develops_only_the_requested_frame(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[int] = []
    raw_frame = Clip.raw_frame

    def read(self: Clip, index: int) -> Any:
        requested.append(index)
        return raw_frame(self, index)

    monkeypatch.setattr(Clip, "raw_frame", read)
    with PreviewSession(_clip(make_mcraw, tmp_path), _recipe()) as session:
        assert session.frame(0).startswith(b"\xff\xd8")
    assert requested == [0]


def test_video_previews_ask_for_a_gpu_decoder_for_their_codec(
    direct_log_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str | None] = []

    def device(ffmpeg: Path, codec: str | None, *, preview: bool = False) -> None:
        asked.append(codec)

    monkeypatch.setattr("mcrawsome.preview.video.decode_device", device)
    with PreviewSession(
        direct_log_video, build_recipe([{"develop": {"input_encoding": "bt709"}}]), PreviewSettings(gpu=False)
    ) as session:
        session.fast(0)
    assert asked
    assert set(asked) == {direct_log_video.name.split("_")[0]}


@pytest.mark.parametrize("policy", ["recorded", "static"])
def test_raw_level_policy_reaches_draft_source_and_refined_preview(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    from mcrawsome.io.metadata import FrameMetadata

    original = Clip.frame_metadata

    def metadata(clip: Clip, index: int) -> FrameMetadata:
        return original(clip, index).model_copy(
            update={
                "black_level": (10.0 + index, 20.0 + index, 30.0 + index, 40.0 + index),
                "white_level": 1000 - 100 * index,
            }
        )

    monkeypatch.setattr(Clip, "frame_metadata", metadata)
    recipe = _recipe(
        develop={
            "level_policy": policy,
            "black_level": 500,
            "white_level": 600,
            "lens_shading": 0,
            "exposure": 0,
        }
    )
    with PreviewSession(tiny_raw, recipe, PreviewSettings(width=160, gpu=False)) as session:
        for i in (2, 0, 1, 2):
            clip, developer = session._raw(i)
            expected_meta = clip.frame_metadata(i if policy == "recorded" else 0)
            expected = Developer(clip.metadata, expected_meta, session._job.neutral, lens_shading=0)
            np.testing.assert_allclose(developer.black, expected.black)
            np.testing.assert_allclose(developer.gain, expected.gain, rtol=1e-6)
            session.keep(i)
            assert session._kept is not None
            np.testing.assert_allclose(
                session._kept[1], expected.develop_half(clip.raw_frame(i).data), rtol=1e-6, atol=1e-6
            )
            session.fast(i, width=160)
            assert session._developer is not None
            assert session._developer.white == expected_meta.white_level
            assert session.source_encoder(i)().jpeg.startswith(b"\xff\xd8")


def test_changing_crop_keeps_draft_source_and_calibration_dimensions_together(tiny_raw: Path) -> None:
    with PreviewSession(tiny_raw, _recipe(), PreviewSettings(width=160, gpu=False)) as session:
        session.fast(0)
        session.set_recipe(_recipe(develop={"crop": "64x32"}), fast=True)
        assert session.full_size == (64, 32)
        assert session.fast(0).size == (32, 16)
        assert session.source_encoder(0)().size == (64, 32)
        session.set_recipe(_recipe(), fast=True)
        assert session.fast(0).size == (64, 32)
