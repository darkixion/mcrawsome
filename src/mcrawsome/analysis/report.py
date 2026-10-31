"""The analysis report: what was measured and decided, in plain language, with suggestions.

Exports and `mcrawsome analyse` take their advice from here, so the wording is the same in both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcrawsome.analysis.measure import SampleStats
from mcrawsome.analysis.resolve import TARGET_KEY, Resolution, auto_exposure
from mcrawsome.pipeline.measure import SdrLight, sdr_brightness_warning
from mcrawsome.pipeline.stabilise import MotionOutcome
from mcrawsome.recipe.schema import Recipe

__all__ = ["Report", "SdrLight", "build_report", "light_advice", "sample_advice"]

CLIPPED_LIMIT = 0.02
WHITE_BALANCE_LIMIT = 15.0  # mired

Section = tuple[str, list[tuple[str, str]]]


@dataclass(frozen=True)
class Report:
    sections: list[Section]
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    # The stops automatic exposure would choose, whatever exposure the Recipe sets (the samples are
    # measured at exposure 0); None when nothing was measured.
    suggested_exposure: float | None = None


def _exposure(recipe: Recipe) -> float:
    exposure = recipe.develop.exposure
    return 0.0 if exposure == "auto" else float(exposure)


def sample_advice(
    stats: SampleStats, recipe: Recipe, white_balance_mired: float, raw: bool = True
) -> tuple[list[str], list[str]]:
    """(warnings, suggestions) from the sample measurements. Highlight recovery works on RAW clips only."""
    warnings: list[str] = []
    suggestions: list[str] = []
    if stats.clipped_share > CLIPPED_LIMIT:
        if not raw:
            warnings.append(
                f"highlights are clipped in {stats.clipped_share:.1%} of the picture (as recorded)"
            )
        elif recipe.develop.highlight_recovery > 0:
            warnings.append(
                f"highlights are clipped in {stats.clipped_share:.1%} of the picture; "
                "highlight recovery is on and rebuilds colour where one channel survives"
            )
        else:
            suggestions.append(
                f"highlights are clipped in {stats.clipped_share:.1%} of the picture: try "
                "--develop.highlight_recovery 1"
            )
    if white_balance_mired > WHITE_BALANCE_LIMIT:
        warnings.append(
            f"the white balance changes during the clip ({white_balance_mired:.0f} mired); "
            f"it is locked to the clip's {recipe.develop.white_balance} value"
        )
    return warnings, suggestions


def light_advice(light: SdrLight) -> list[str]:
    warning = sdr_brightness_warning(light)
    return [warning] if warning else []


def build_report(
    resolution: Resolution,
    white_balance_mired: float,
    light: SdrLight,
    motion: MotionOutcome,
    raw: bool = True,
) -> Report:
    recipe, stats = resolution.recipe, resolution.stats
    exposure = _exposure(recipe)
    auto = bool(resolution.measurements.get("exposure_auto"))
    sections: list[Section] = [
        ("Exposure", [
            ("Exposure", f"{exposure:+.1f} stops ({'automatic' if auto else 'set'})"),
            ("Scene key", f"{stats.key:.3f} (target {TARGET_KEY})" if stats else "not measured"),
        ]),
        ("Clipping", [
            ("Clipped", f"{stats.clipped_share:.1%} of the picture" if stats else "not measured"),
            ("Highlight recovery", f"{recipe.develop.highlight_recovery:g}"),
        ]),
        ("Noise", [("At mid-grey", f"{stats.noise_mid_grey(exposure):.1%}" if stats else "not measured")]),
        ("White balance", [
            ("Change during the clip", f"{white_balance_mired:.0f} mired"),
            ("Locked to", recipe.develop.white_balance),
        ]),
    ]  # fmt: skip
    brightness = [("Median code", f"{light.median_code:.2f}"), ("90th percentile", f"{light.p90_code:.2f}")]
    sections.append(("Brightness", brightness))
    m = motion.measurements
    rows = [("Stabilised", "yes" if m.get("stabilised") else "no")]
    if "shake_removed" in m:
        rows.append(("Shake removed", f"{max(m['shake_removed'], 0.0):.0%}"))
    if m.get("suggested_crop"):
        rows.append(("Suggested crop", f"{m['suggested_crop']}"))
    sections.append(("Stabilisation", rows))
    warnings = list(resolution.notes)
    suggestions: list[str] = []
    if stats:
        sample_warnings, suggestions = sample_advice(stats, recipe, white_balance_mired, raw)
        warnings += sample_warnings
    warnings += light_advice(light) + list(motion.notes)
    suggested = auto_exposure(stats)[0] if stats else None
    return Report(sections, warnings, suggestions, suggested)
