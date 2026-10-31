"""Fast drafts and paired looks use one reduced development."""

import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast

import cv2
import numpy as np
import pytest

from mcrawsome.develop.raw import Developer
from mcrawsome.preview.backend import PreviewBackend
from mcrawsome.preview.session import PreviewSession
from mcrawsome.preview.worker import Reply, _Loop
from mcrawsome.recipe.load import build_recipe


class _Connection:
    def __init__(self) -> None:
        self.replies: list[Reply] = []

    def poll(self, timeout: float) -> bool:
        return False

    def send(self, reply: Reply) -> None:
        self.replies.append(reply)


def decode(jpeg: bytes) -> np.ndarray:
    return np.asarray(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR))


@pytest.mark.parametrize("look", ["neutral", "gorgeous", "gritty"])
@pytest.mark.parametrize("stops", [-1.0, 1.0])
def test_fast_size_and_mean_colour(
    make_mcraw: Callable[..., Path], tmp_path: Path, look: str, stops: float, preview_compute: PreviewBackend
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667, 83_333_334])
    recipe = build_recipe(
        [
            {
                "look": look,
                "develop": {"exposure": stops},
                "motion": {"stabilise": "off"},
                "effects": {
                    "grain": 0,
                    "halation": 0,
                    "streak": 0,
                    "vignette": 0,
                    "bloom": 0,
                    "clarity": 0,
                    "soften": 0,
                    "corner_soft": 0,
                    "weave": 0,
                },
            }
        ]
    )
    with PreviewSession(clip, recipe) as session:
        session.backend.close()
        session.backend = preview_compute
        fast = decode(session.fast(1, width=32).jpeg)
        full = decode(session.refined(1).jpeg)
    assert fast.shape == (16, 32, 3)
    np.testing.assert_allclose(fast.mean(axis=(0, 1)), full.mean(axis=(0, 1)), atol=5)


def test_fast_reuses_one_development_for_a_repeated_frame(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    calls = 0
    original = PreviewBackend.develop

    def develop(
        self: PreviewBackend, developer: Developer, raw: np.ndarray, step: int, width: int
    ) -> np.ndarray:
        nonlocal calls
        calls += 1
        return original(self, developer, raw, step, width)

    monkeypatch.setattr(PreviewBackend, "develop", develop)
    with PreviewSession(clip, build_recipe([{"develop": {"exposure": 0}}])) as session:
        first = session.fast(0)
        assert decode(first.jpeg) is not None
        session.fast(0)
        assert calls == 1


def test_playback_sends_at_most_three_ahead_and_never_refines(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in range(6)])
    with PreviewSession(clip, build_recipe([{"develop": {"exposure": 0}}])) as session:
        conn = _Connection()
        loop = _Loop(cast(Connection, conn), session)
        try:
            loop._answer(
                [{"op": "frame", "seq": 1, "index": 1, "playback": True, "ahead": 3}],
                time.perf_counter(),
            )
            assert [header["index"] for header, _ in conn.replies] == [1, 2, 3]
            assert [header["buffer_end"] for header, _ in conn.replies] == [False, False, True]
            for _, data in conn.replies:
                assert decode(data) is not None
            assert loop.refine_due is None
        finally:
            loop.close()


def test_fast_omits_effects_and_invalidates_white_balance(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"look": "neutral", "develop": {"exposure": 0}}])
    with PreviewSession(clip, recipe) as session:
        session.set_recipe(recipe, fast=True)
        first = session.fast(0).jpeg
        session.set_recipe(
            build_recipe(
                [
                    {
                        "look": "neutral",
                        "develop": {"exposure": 0},
                        "effects": {"grain": 0.1, "halation": 2, "weave": 10},
                    }
                ]
            ),
            fast=True,
        )
        assert session.fast(0).jpeg == first
        session.set_recipe(
            build_recipe([{"look": "neutral", "develop": {"exposure": 0, "white_level": 500}}]), fast=True
        )
        assert session.fast(0).jpeg != first


def test_fast_cube_matches_refined_colour_after_a_grade_change(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    with PreviewSession(clip, build_recipe([{"look": "neutral"}])) as session:
        session.set_recipe(
            build_recipe(
                [{"look": "neutral", "develop": {"exposure": 1}, "grade": {"warmth": 0.15, "sat": 1.3}}]
            ),
            fast=True,
        )
        assert len(session._table) == 33**3
        fast = decode(session.fast(1).jpeg)
        full = decode(session.refined(1).jpeg)
        np.testing.assert_allclose(fast.mean(axis=(0, 1)), full.mean(axis=(0, 1)), atol=5)


def test_fast_draft_respects_the_sessions_preview_limit(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    from mcrawsome.preview.session import PreviewSettings

    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    with PreviewSession(clip, build_recipe([]), PreviewSettings(width=32)) as session:
        assert decode(session.fast(0).jpeg).shape == (16, 32, 3)
