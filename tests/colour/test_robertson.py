"""Robertson's correlated colour temperature, exactly as colour-science 0.4.7 computed it before it was
replaced (the values below are its results)."""

import pytest

from mcrawsome.colour.robertson import cct_to_uv, uv_to_cct

CCT_TO_UV = [
    ((1800, -0.01), (0.32345986978481717, 0.35017751635641997)),
    ((2850, 0.0), (0.25619385964912283, 0.3495973684210526)),
    ((3200, 0.004), (0.2423859370051524, 0.34847046431917567)),
    ((5000, -0.002), (0.2128214032917506, 0.32169309116833955)),
    ((5500, 0.0), (0.20693272727272727, 0.3182836363636363)),
    ((6500, 0.0083333), (0.19374144805635776, 0.31522108620243205)),
    ((7500, 0.01), (0.18779306560683293, 0.30928813802740995)),
    ((12000, -0.02), (0.2063027633240657, 0.28002358082688383)),
    ((25000, 0.0), (0.18293, 0.27407)),
]
UV_TO_CCT = [
    ((0.19374137599823, 0.315221043940594), (6500.016287949804, 0.008333328983859935)),
    ((0.2, 0.3), (7738.272612738262, -0.005292351844429211)),
    ((0.25, 0.34), (3092.547395371112, -0.006833423872512478)),
    ((0.18, 0.27), (41671.76470204225, 0.001690445209869123)),
    ((0.33, 0.36), (1732.6616708384054, -0.0003912678623173761)),
    ((0.3, 0.35), (2075.547699937687, -0.008508321181538712)),
    ((0.21, 0.33), (4760.594992600587, 0.006068471422476255)),
    ((0.36, 0.38), (1666.6666666666667, 0.01929383992952209)),  # warmer than the last line
    ((0.15, 0.25), (100000.0, 0.025787511618256067)),  # bluer than the first
]


@pytest.mark.parametrize(("given", "expected"), CCT_TO_UV)
def test_a_temperature_and_tint_give_colour_sciences_uv(
    given: tuple[float, float], expected: tuple[float, float]
) -> None:
    assert cct_to_uv(*given) == expected


@pytest.mark.parametrize(("given", "expected"), UV_TO_CCT)
def test_a_uv_gives_colour_sciences_temperature_and_duv(
    given: tuple[float, float], expected: tuple[float, float]
) -> None:
    assert uv_to_cct(*given) == expected


def test_the_two_directions_agree() -> None:
    kelvin, duv = uv_to_cct(*cct_to_uv(4300, 0.003))
    assert kelvin == pytest.approx(4300, abs=0.5)
    assert duv == pytest.approx(0.003, abs=1e-6)
