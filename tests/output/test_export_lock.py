"""One export at a time per target: the work folder beside the output is locked for the whole export."""

import errno
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SYNTHETIC_STEP, write_mcraw

from mcrawsome.errors import McError
from mcrawsome.output import chunks
from mcrawsome.output.chunks import WorkFolder, export_lock
from mcrawsome.output.dng_export import export_dng
from mcrawsome.output.export import ExportResult
from mcrawsome.output.exr_export import export_exr
from mcrawsome.output.mcraw_export import export_mcraw
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.schema import Recipe

EXPORTS: dict[str, tuple[Callable[..., ExportResult], str]] = {
    "exr": (export_exr, ""),
    "dng": (export_dng, ""),
    "mcraw": (export_mcraw, ".mcraw"),
}


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    return write_mcraw(tmp_path / "synthetic.mcraw", [i * SYNTHETIC_STEP for i in range(3)])


def _recipe(kind: str) -> Recipe:
    return build_recipe(
        [{"output": {"kind": kind}, "develop": {"exposure": 0}, "motion": {"stabilise": "off"}}]
    )


def _lock_free(output: Path) -> bool:
    try:
        with export_lock(output):
            return True
    except McError:
        return False


@pytest.mark.parametrize("kind", list(EXPORTS))
def test_a_second_export_to_the_same_target_fails_at_once(clip: Path, tmp_path: Path, kind: str) -> None:
    export, suffix = EXPORTS[kind]
    output = tmp_path / f"out{suffix}"
    recipe = _recipe(kind)
    rendering, release = threading.Event(), threading.Event()
    outcome: list[object] = []

    def held(phase: str, done: int, total: int) -> None:
        if phase == "rendering":
            rendering.set()
            assert release.wait(60)

    def first() -> None:
        try:
            outcome.append(export(clip, output, recipe, workers=1, progress=held))
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert rendering.wait(60)
        work = sorted(WorkFolder(output).path.iterdir())
        with pytest.raises(McError) as caught:
            export(clip, output, recipe, workers=1, restart=True)
        assert caught.value.code == "output-exists"
        assert f"another MCRAWsome process (pid {os.getpid()}) is exporting to {output}" in str(caught.value)
        assert sorted(WorkFolder(output).path.iterdir()) == work  # the first export's work is untouched
    finally:
        release.set()
        thread.join()
    assert isinstance(outcome[0], ExportResult), outcome
    assert _lock_free(output)
    assert not chunks.lock_path(output).exists()  # nothing is left beside the output


@pytest.mark.parametrize("kind", list(EXPORTS))
def test_the_lock_is_released_after_a_failure_and_a_cancel(clip: Path, tmp_path: Path, kind: str) -> None:
    export, suffix = EXPORTS[kind]
    output = tmp_path / f"out{suffix}"

    def cancel(phase: str, done: int, total: int) -> None:
        if phase == "rendering":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        export(clip, output, _recipe(kind), workers=1, progress=cancel)
    assert _lock_free(output)
    with pytest.raises(McError):
        export(tmp_path / "missing.mcraw", output, _recipe(kind), workers=1)
    assert _lock_free(output)


@pytest.mark.parametrize(
    ("where", "number", "code"),
    [("work", errno.EACCES, "output-unwritable"), ("none", errno.ENOSPC, "disk-full"),
     ("input", errno.EIO, "input-unreadable")],
)  # fmt: skip
def test_an_os_error_in_an_export_gets_a_code(
    clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, number: int, code: str
) -> None:
    output = tmp_path / "out.mcraw"
    name = {"work": str(WorkFolder(output).path / "manifest.part"), "input": str(clip), "none": None}[where]

    def save(self: WorkFolder, manifest: object) -> None:
        raise OSError(number, os.strerror(number), name)

    monkeypatch.setattr(WorkFolder, "save", save)
    with pytest.raises(McError) as caught:
        export_mcraw(clip, output, _recipe("mcraw"), workers=1)
    assert caught.value.code == code
    assert str(caught.value).startswith(f"{clip.name}: ")
    assert _lock_free(output)
