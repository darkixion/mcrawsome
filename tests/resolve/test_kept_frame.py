"""The kept frame of a stabilised timeline, frame by frame, for the unstabilised preview."""

import threading
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mcrawsome.motion.geometry import intrinsics, quat_from_rotvec, quat_to_matrix, warp_maps, zoom_matrix
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.recipe.schema import Recipe
from mcrawsome.resolve import kept_frame as module
from mcrawsome.resolve.gyro import plan_timeline_clip
from mcrawsome.resolve.kept_frame import kept_corners, kept_frame

WIDTH, HEIGHT, FOCAL = 400, 300, 350.0


def plan(*turns: list[list[float]], zoom: float = 1.25, rotate_180: bool = False) -> StabilisationPlan:
    """One frame per entry of `turns`, each a list of rotation vectors: one per row band."""
    rotations = np.array(
        [[quat_to_matrix(quat_from_rotvec(np.array(band))) for band in frame] for frame in turns]
    )
    count = len(rotations)
    return StabilisationPlan(
        rotations.astype(np.float32), np.tile([1.0, 0, 0, 0], (count, 1)), FOCAL, zoom, WIDTH, HEIGHT,
        0, 0, 0, 1.0, rotate_180,
    )  # fmt: skip


def test_with_no_correction_the_kept_frame_is_the_centred_crop() -> None:
    [corners] = kept_corners(plan([[0.0, 0.0, 0.0]]))
    margin = (1 - 1 / 1.25) / 2
    expected = np.array(
        [[margin, margin], [1 - margin, margin], [1 - margin, 1 - margin], [margin, 1 - margin]]
    )
    assert corners == pytest.approx(expected)


def test_the_kept_frame_comes_from_where_the_render_takes_its_corner() -> None:
    turn = [0.01, -0.02, 0.03]
    [corners] = kept_corners(plan([turn]))
    mx, my = warp_maps(plan([turn]).rotations[0].astype(np.float64), FOCAL, 1.25, WIDTH, HEIGHT)
    assert corners[0] == pytest.approx([mx[0, 0] / WIDTH, my[0, 0] / HEIGHT], abs=1e-5)


def test_a_recording_held_upside_down_and_a_turned_display_turn_the_outline_with_the_picture() -> None:
    turn = [[0.01, -0.02, 0.03]]
    [upright] = kept_corners(plan(turn))
    [flipped] = kept_corners(plan(turn, rotate_180=True))
    # Planned in the sensor's orientation: shown upright, the same correction is turned half a turn.
    assert flipped == pytest.approx((1 - upright)[[2, 3, 0, 1]], abs=1e-6)
    x, y = upright[:, 0], upright[:, 1]
    assert kept_corners(plan(turn), 90)[0] == pytest.approx(np.stack([1 - y, x], -1), abs=1e-6)
    assert kept_corners(plan(turn), 180)[0] == pytest.approx(np.stack([1 - x, 1 - y], -1), abs=1e-6)
    assert kept_corners(plan(turn), 270)[0] == pytest.approx(np.stack([y, 1 - x], -1), abs=1e-6)


def test_rolling_shutter_rows_give_each_corner_the_pose_of_the_row_it_comes_from() -> None:
    turn = [0.0, 0.03, 0.0]
    [same] = kept_corners(plan([turn] * 12))
    assert same == pytest.approx(kept_corners(plan([turn]))[0], abs=1e-6)
    ramp = [[0.0, value, 0.0] for value in np.linspace(0.03, -0.03, 12)]  # the camera turned during readout
    [rows] = kept_corners(plan(ramp))
    # Resolve takes the pose at the sensor row a point comes from, interpolated between the twelve rows.
    k = intrinsics(FOCAL, WIDTH, HEIGHT)
    rotations = plan(ramp).rotations[0].astype(np.float64)
    inverse = np.linalg.inv(zoom_matrix(1.25, WIDTH, HEIGHT) @ k @ rotations @ np.linalg.inv(k))
    for corner, (x, y) in zip(
        rows * [WIDTH, HEIGHT], [(0, 0), (WIDTH, 0), (WIDTH, HEIGHT), (0, HEIGHT)], strict=True
    ):
        position = corner[1] / HEIGHT * 11
        low = int(position)
        pose = inverse[low] * (low + 1 - position) + inverse[low + 1] * (position - low)
        source = pose @ [x, y, 1.0]
        assert source[:2] / source[2] == pytest.approx(corner, abs=0.01)
    assert abs(rows[0, 0] - rows[3, 0]) > 0.02  # sheared: the top and bottom rows are turned back differently


def _recorded_clip(make_mcraw: Any, path: Path) -> Path:
    from conftest import RECORDED_CONTAINER, RECORDED_FRAME, SYNTHETIC_STEP, recorded_motion

    stamps = [1_000_000_000 + i * SYNTHETIC_STEP for i in range(48)]
    return make_mcraw(  # type: ignore[no-any-return]
        path,
        stamps,
        container=RECORDED_CONTAINER,
        frame_fields=RECORDED_FRAME,
        motion=recorded_motion(stamps[0], 2.0),
    )


def test_a_clip_is_planned_once_for_each_setting_as_a_send_plans_it(
    make_mcraw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    monkeypatch.setattr(module, "_kept", type(module._kept)())
    planned: list[dict[str, Any]] = []

    def planning(*args: Any, **kwargs: Any) -> Any:
        planned.append(kwargs)
        return plan_timeline_clip(*args, **kwargs)

    monkeypatch.setattr(module, "plan_timeline_clip", planning)
    recipe = Recipe.model_validate(
        {"motion": {"stabilise": "on", "optical_correction": False}, "develop": {"rotation": 90}}
    )
    result = kept_frame(path, recipe, threading.Event())
    assert result.reason is None
    assert len(result.corners) == 48
    assert all(len(frame) == 8 and all(-0.1 < value < 1.1 for value in frame) for frame in result.corners)
    assert planned[0]["display_rotation"] == 90
    unrelated = recipe.model_copy(update={"develop": recipe.develop.model_copy(update={"exposure": 1.0})})
    assert kept_frame(path, unrelated, threading.Event()) == result
    assert len(planned) == 1  # a colour change keeps the plan
    tripod = Recipe.model_validate(
        {"motion": {"stabilise": "on", "optical_correction": False, "mode": "tripod"}}
    )
    kept_frame(path, tripod, threading.Event())
    assert len(planned) == 2


def test_a_quick_first_look_is_planned_without_image_assisted_refinement(
    make_mcraw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    refined: list[bool] = []

    def planning(source: Path, recipe: Recipe, *args: Any, **kwargs: Any) -> Any:
        refined.append(recipe.motion.optical_correction)
        return plan_timeline_clip(source, recipe, *args, **kwargs)

    monkeypatch.setattr(module, "plan_timeline_clip", planning)
    values = Recipe.model_validate({"motion": {"stabilise": "on", "mode": "tripod"}})
    quick = kept_frame(path, values, threading.Event(), quick=True)
    full = kept_frame(path, values, threading.Event())
    assert refined == [False, True]  # planned apart: the send's plan is the refined one
    assert len(quick.corners) == len(full.corners) == 48


@pytest.mark.parametrize(
    ("motion", "develop", "reason"),
    [
        ({"stabilise": "on", "strength": 0}, {}, "strength is zero"),
        ({"stabilise": "on", "mode": "rolling_shutter", "bands": 1}, {}, "Rolling shutter compensation on"),
        ({"stabilise": "off"}, {}, "Stabilise is off"),
        ({"stabilise": "on"}, {"mount_crop": "auto"}, "uncropped media"),
    ],
)
def test_settings_a_send_would_refuse_say_why_the_frame_cannot_move(
    make_mcraw: Any, tmp_path: Path, motion: dict[str, Any], develop: dict[str, Any], reason: str
) -> None:
    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    result = kept_frame(
        path, Recipe.model_validate({"motion": motion, "develop": develop}), threading.Event()
    )
    assert result.corners == []
    assert result.reason is not None
    assert reason in result.reason


def test_a_page_that_stops_waiting_stops_the_planning(make_mcraw: Any, tmp_path: Path) -> None:
    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        kept_frame(path, Recipe.model_validate({"motion": {"stabilise": "on", "crop": 1.3}}), stop)


def test_a_send_after_the_preview_reuses_its_plan(
    make_mcraw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.resolve import gyro

    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    values = Recipe.model_validate({"motion": {"stabilise": "on", "optical_correction": False}})
    shown = kept_frame(path, values, threading.Event())
    monkeypatch.setattr(gyro, "plan_raw_clip", lambda *_a, **_kw: pytest.fail("planned again"))
    recipe, rows = gyro.timeline_recipe(values, path.name)  # as a send plans the clip
    told: list[str] = []
    outcome = gyro.plan_timeline_clip(path, recipe, rows, tmp_path / "send", stage=told.append)
    assert told == ["Reusing the stabilisation planned earlier"]
    assert outcome.plan_path == tmp_path / "send" / "stabilisation.npz"
    assert outcome.gyro is not None
    sent = kept_corners(StabilisationPlan.load(outcome.plan_path)).reshape(-1, 8)
    assert sent == pytest.approx(np.array(shown.corners), abs=1e-4)


def _held_planner(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Path], threading.Event, threading.Event]:
    """A planner that waits for `finish` once it has `started`; the folders it planned in."""
    from mcrawsome.pipeline.stabilise import MotionOutcome
    from mcrawsome.resolve import gyro

    calls: list[Path] = []
    started, finish = threading.Event(), threading.Event()

    def planner(_source: Path, _recipe: Recipe, _workers: int, folder: Path, **_kw: Any) -> MotionOutcome:
        calls.append(folder)
        started.set()
        assert finish.wait(5)
        (folder / "stabilisation.npz").write_bytes(b"plan")
        return MotionOutcome(folder / "stabilisation.npz")

    monkeypatch.setattr(gyro, "plan_raw_clip", planner)
    return calls, started, finish


def test_a_plan_being_made_is_waited_for_rather_than_made_twice(
    make_mcraw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.resolve import gyro

    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    recipe, rows = gyro.timeline_recipe(Recipe.model_validate({"motion": {"stabilise": "on"}}), path.name)
    calls, started, finish = _held_planner(monkeypatch)
    for name in ("preview", "send"):
        (tmp_path / name).mkdir()
    told: list[str] = []
    preview = threading.Thread(
        target=gyro.plan_timeline_clip, args=(path, recipe, rows, tmp_path / "preview")
    )
    send = threading.Thread(
        target=gyro.plan_timeline_clip,
        args=(path, recipe, rows, tmp_path / "send"),
        kwargs={"stage": told.append},
    )
    preview.start()
    assert started.wait(5)
    send.start()
    for _ in range(500):
        if told:
            break
        threading.Event().wait(0.01)
    assert told == ["Waiting for the same stabilisation, already being planned"]
    finish.set()
    preview.join(5)
    send.join(5)
    assert calls == [tmp_path / "preview"]
    assert told[1:] == ["Reusing the stabilisation planned earlier"]
    assert (tmp_path / "send" / "stabilisation.npz").read_bytes() == b"plan"
    assert not gyro._planning


def test_waiting_for_a_plan_can_be_given_up(
    make_mcraw: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.resolve import gyro

    path = _recorded_clip(make_mcraw, tmp_path / "recorded.mcraw")
    recipe, rows = gyro.timeline_recipe(Recipe.model_validate({"motion": {"stabilise": "on"}}), path.name)
    _calls, started, finish = _held_planner(monkeypatch)
    (tmp_path / "preview").mkdir()
    preview = threading.Thread(
        target=gyro.plan_timeline_clip, args=(path, recipe, rows, tmp_path / "preview")
    )
    preview.start()
    assert started.wait(5)

    def stopped() -> None:
        raise CancelledError

    with pytest.raises(CancelledError):
        gyro.plan_timeline_clip(path, recipe, rows, tmp_path / "send", check=stopped)
    finish.set()
    preview.join(5)
    assert not gyro._planning
