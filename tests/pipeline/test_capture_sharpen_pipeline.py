"""Capture detail is restored once, after NR and before geometric/output processing."""

from pathlib import Path

from mcrawsome.output.export import (
    frame_job,
    read_source,
)
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.schema import Recipe


def recipe() -> Recipe:
    return build_recipe(
        [
            {
                "develop": {"exposure": 0, "capture_sharpening": 0.8, "capture_radius": 1.1},
                "motion": {"stabilise": "off"},
                "denoise": {"luminance": 0.4, "chroma": 2},
            }
        ]
    )


def test_log_does_not_acquire_raw_sharpening(synthetic_video: Path) -> None:
    settings = recipe()
    facts, _ = read_source(synthetic_video, settings, audio=False)
    job = frame_job(synthetic_video, settings, facts)
    assert job.capture_sharpening == 0


def test_sharpening_does_not_invalidate_the_draft_raw_cache(tiny_raw: Path) -> None:
    from mcrawsome.preview.session import PreviewSession

    off = build_recipe([{"develop": {"exposure": 0}, "motion": {"stabilise": "off"}}])
    with PreviewSession(tiny_raw, off) as session:
        before = session.fast(1).jpeg
        kept = session._fast_kept
        session.set_recipe(recipe())
        assert session._fast_kept is kept
        assert session._job.capture_sharpening == 0.8
        assert session.fast(1).jpeg == before
        assert session.refined(1).jpeg.startswith(b"\xff\xd8")
