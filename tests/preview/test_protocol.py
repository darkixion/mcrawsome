"""Deterministic checks of queued preview operations and stale measurements."""

import time
from collections.abc import Callable
from concurrent.futures import Future
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, cast

import pytest

from mcrawsome.preview.session import PreviewSession, Refined
from mcrawsome.preview.worker import Reply, _Loop
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.schema import Recipe


class _Connection:
    def __init__(self) -> None:
        self.replies: list[Reply] = []

    def poll(self, timeout: float) -> bool:
        return False

    def send(self, reply: Reply) -> None:
        self.replies.append(reply)


@pytest.fixture
def clip(make_mcraw: Callable[..., Path], tmp_path: Path) -> Path:
    return make_mcraw(tmp_path / "synthetic.mcraw", [i * 41_666_667 for i in range(6)])


@pytest.mark.parametrize("old_failed", [False, True])
def test_an_old_auto_measurement_does_not_block_the_new_settings(
    clip: Path, monkeypatch: pytest.MonkeyPatch, old_failed: bool
) -> None:
    old = build_recipe([{"develop": {"exposure": "auto", "white_balance": "median"}}])
    new = build_recipe([{"develop": {"exposure": "auto", "white_balance": "first"}}])
    with PreviewSession(clip, old) as session:
        old_measurer = session.auto_measurer()
        assert old_measurer is not None
        old_key, _ = old_measurer
        session.set_recipe(new)
        new_measurer = session.auto_measurer()
        assert new_measurer is not None
        new_key, _ = new_measurer
        assert old_key != new_key
        measured: Future[float] = Future()
        if old_failed:
            measured.set_exception(ValueError("old settings could not be measured"))
        else:
            measured.set_result(0.75)
        loop = _Loop(cast(Connection, _Connection()), session)
        loop.measuring = (measured, old_key)
        loop.measure_due = 0.0
        monkeypatch.setattr(
            session,
            "auto_measurer",
            lambda **kwargs: (new_key, lambda: 1.25) if session.auto_pending else None,
        )
        try:
            loop._background()
            assert loop.auto_error is None, "the failure belongs to superseded develop settings"
            assert loop.measuring is not None, "the current settings must still be measured"
            loop.measuring[0].result(timeout=2)
            loop._background()
            assert session.auto_stops() == 1.25
            assert not session.auto_pending
        finally:
            loop.background.shutdown(wait=True)


def test_a_failure_for_the_current_auto_settings_is_reported(clip: Path) -> None:
    recipe = build_recipe([{"develop": {"exposure": "auto"}}])
    with PreviewSession(clip, recipe) as session:
        measurer = session.auto_measurer()
        assert measurer is not None
        key, _ = measurer
        measured: Future[float] = Future()
        measured.set_exception(ValueError("current settings could not be measured"))
        loop = _Loop(cast(Connection, _Connection()), session)
        loop.measuring = (measured, key)
        try:
            loop._background()
            assert loop.auto_error is not None
            assert "current settings could not be measured" in loop.auto_error
            assert loop.measuring is None
        finally:
            loop.background.shutdown(wait=True)


def test_a_failed_recipe_is_not_followed_by_a_successful_stale_table(
    clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = build_recipe([{"develop": {"exposure": 0}}])
    new = build_recipe([{"look": "gritty", "develop": {"exposure": 0}}])
    with PreviewSession(clip, old) as session:

        def fail(recipe: Recipe) -> None:
            raise ValueError("changed look cannot be read")

        monkeypatch.setattr(session, "set_recipe", fail)
        connection = _Connection()
        loop = _Loop(cast(Connection, connection), session)
        try:
            loop._answer(
                [{"seq": 1, "op": "recipe", "recipe": new}, {"seq": 2, "op": "table"}], time.perf_counter()
            )
            assert all(header["kind"] == "error" for header, _ in connection.replies)
            assert {header["seq"] for header, _ in connection.replies} == {1, 2}
        finally:
            loop.background.shutdown(wait=True)


def test_recipes_without_table_requests_still_coalesce(clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = build_recipe([{"develop": {"exposure": 0}}])
    changes = [build_recipe([{"develop": {"exposure": exposure}}]) for exposure in [1, 2, 3]]
    with PreviewSession(clip, old) as session:
        applied: list[Recipe] = []
        original = session.set_recipe

        def track(recipe: Recipe) -> None:
            applied.append(recipe)
            original(recipe)

        monkeypatch.setattr(session, "set_recipe", track)
        connection = _Connection()
        loop = _Loop(cast(Connection, connection), session)
        try:
            loop._answer(
                [
                    {"seq": seq, "op": "recipe", "recipe": recipe}
                    for seq, recipe in enumerate(changes, start=1)
                ],
                time.perf_counter(),
            )
            assert applied == [changes[-1]]
            drafts = [header for header, _ in connection.replies if header["kind"] == "draft"]
            assert len(drafts) == 1
            assert drafts[0]["seq"] == 3
        finally:
            loop.background.shutdown(wait=True)


@pytest.mark.parametrize("failed", [False, True])
def test_an_old_refined_frame_does_not_replace_a_new_measured_exposure(
    clip: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    recipe = build_recipe([{"develop": {"exposure": "auto"}}])
    with PreviewSession(clip, recipe) as session:
        old_jpeg = session.frame(0)
        measurer = session.auto_measurer()
        assert measurer is not None
        key, _ = measurer
        measured: Future[float] = Future()
        measured.set_result(1.25)
        refining: Future[Refined] = Future()
        connection = _Connection()
        loop = _Loop(cast(Connection, connection), session)
        loop.shown = (1, 0)
        loop.newest = 1
        loop.measuring = (measured, key)
        loop.refining = (refining, 1, 0, time.perf_counter())
        monkeypatch.setattr(session, "auto_measurer", lambda **kwargs: None)
        try:
            loop._background()
            assert connection.replies[-1][0]["kind"] == "draft"
            assert connection.replies[-1][0]["exposure"]["stops"] == 1.25
            assert connection.replies[-1][1] != old_jpeg
            assert loop.refining is not None
            assert loop.refining[0] is refining
            loop.refine_due = None
            connection.replies.clear()
            if failed:
                refining.set_exception(ValueError("superseded refinement failed"))
            else:
                refining.set_result(Refined(old_jpeg))
            loop._background()
            assert connection.replies == [], "superseded refinement must not reappear"
        finally:
            loop.background.shutdown(wait=True)


def _sample_request(seq: int, purpose: str) -> dict[str, Any]:
    return {"seq": seq, "op": "sample", "purpose": purpose, "index": 0, "width": 16, "recipe": Recipe()}
