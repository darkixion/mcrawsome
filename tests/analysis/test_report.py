import pytest

from mcrawsome.analysis.measure import SampleStats
from mcrawsome.analysis.report import SdrLight, build_report, light_advice, sample_advice
from mcrawsome.analysis.resolve import Resolution
from mcrawsome.pipeline.stabilise import MotionOutcome
from mcrawsome.recipe.schema import Recipe


def _stats(noise: float = 0.005, clipped: float = 0.0) -> SampleStats:
    return SampleStats(key=0.08, clipped_share=clipped, noise_rel=noise, noise_level=0.09, frames=24)


def _recipe(**develop: object) -> Recipe:
    return Recipe.model_validate({"develop": {"exposure": 1.0, **develop}})


def test_noise_does_not_suggest_removed_looks_or_denoise() -> None:
    _, suggestions = sample_advice(_stats(noise=0.05), _recipe(), 0.0)
    assert not any("gorgeous_night" in s or "--denoise" in s for s in suggestions)


def test_a_clean_clip_gets_no_advice() -> None:
    warnings, suggestions = sample_advice(_stats(), _recipe(), 0.0)
    assert warnings == []
    assert suggestions == []


def test_clipping_with_and_without_highlight_recovery() -> None:
    warnings, suggestions = sample_advice(_stats(clipped=0.05), _recipe(), 0.0)
    assert any("clipped" in w and "highlight recovery is on" in w for w in warnings)
    _, suggestions = sample_advice(_stats(clipped=0.05), _recipe(highlight_recovery=0.0), 0.0)
    assert any("--develop.highlight_recovery 1" in s for s in suggestions)


def test_a_drifting_white_balance_is_reported() -> None:
    warnings, _ = sample_advice(_stats(), _recipe(), 22.0)
    assert any("white balance changes" in w for w in warnings)


@pytest.mark.parametrize(("median", "fires"), [(0.1, True), (0.4, False), (0.8, True)])
def test_sdr_brightness(median: float, fires: bool) -> None:
    assert bool(light_advice(SdrLight(median_code=median, p90_code=0.9))) is fires


def test_the_report_has_every_section() -> None:
    resolution = Resolution(_recipe(), [], {"exposure_auto": True}, _stats())
    motion = MotionOutcome(None, ["not stabilised: the camera barely moves"])
    report = build_report(resolution, 0.0, SdrLight(0.4, 0.8), motion)
    titles = [title for title, _ in report.sections]
    assert titles == ["Exposure", "Clipping", "Noise", "White balance", "Brightness", "Stabilisation"]
    assert any("barely moves" in w for w in report.warnings)


def test_clipping_in_a_video_does_not_promise_highlight_recovery() -> None:
    warnings, suggestions = sample_advice(_stats(clipped=0.05), _recipe(), 0.0, raw=False)
    assert any("clipped" in w for w in warnings)
    assert not any("highlight recovery" in text for text in warnings + suggestions)


def test_the_suggested_exposure_is_the_automatic_one_while_exposure_is_set() -> None:
    stats = SampleStats(key=0.13, clipped_share=0.0, noise_rel=0.005, noise_level=0.09, frames=24)
    motion = MotionOutcome(None, [])
    report = build_report(Resolution(_recipe(), stats=stats), 0.0, SdrLight(0.4, 0.8), motion)
    assert dict(report.sections[0][1])["Exposure"] == "+1.0 stops (set)"
    assert report.suggested_exposure == 0.3
    assert build_report(Resolution(_recipe()), 0.0, SdrLight(0.4, 0.8), motion).suggested_exposure is None
