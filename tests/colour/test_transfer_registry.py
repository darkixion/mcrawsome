"""The transfer registry, tested against the makers' published reference values."""

import numpy as np
import pytest

from mcrawsome.colour import transfer as tf

CV10 = 1 / 1023  # one 10-bit full-range code value


def _ten_bit(code: float) -> float:
    return code / 1023


# (transfer id, linear input, expected code value, tolerance). Page numbers are the PDF's own pages;
# "recalled" marks a value whose document is not among the owner's copies.
PUBLISHED = [
    # Sony, Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3, page 6: 10-bit code values.
    ("s-log3", 0.0, _ten_bit(95), 0.5 * CV10),
    ("s-log3", 0.18, _ten_bit(420), 0.5 * CV10),
    ("s-log3", 0.90, _ten_bit(598), 0.5 * CV10),
    # Panasonic, V-Log/V-Gamut Reference Manual, Rev.1.0 (2014-11-28), figure 2.2, page 4.
    ("v-log", 0.0, _ten_bit(128), 0.5 * CV10),
    ("v-log", 0.18, _ten_bit(433), 0.5 * CV10),
    ("v-log", 0.90, _ten_bit(602), 0.5 * CV10),
    # Fujifilm, F-Log Data Sheet Ver.1.2, section 2-2, page 2.
    ("f-log", 0.0, _ten_bit(95), 0.5 * CV10),
    ("f-log", 0.18, _ten_bit(470), 0.5 * CV10),
    ("f-log", 0.90, _ten_bit(705), 0.5 * CV10),
    # Fujifilm, F-Log2 Data Sheet Ver.1.1, section 2-2, page 2.
    ("f-log2", 0.0, _ten_bit(95), 0.5 * CV10),
    ("f-log2", 0.18, _ten_bit(400), 0.5 * CV10),
    ("f-log2", 0.90, _ten_bit(570), 0.5 * CV10),
    # ARRI, ALEXA Log C Curve Usage in VFX (2017-03-09): 18 % grey at 0.391 = 400/1023 (page 3), black
    # at 0.0928 (page 8) and f = 0.092809 at EI 800 (page 9).
    ("logc3", 0.0, 0.092809, 5e-7),
    ("logc3", 0.0, 0.0928, 5e-5),
    ("logc3", 0.18, 400 / 1023, 5e-4),
    # Canon, Canon Log Gamma Curves white paper (2018-11-01), appendix [4], page 10: full-range 10-bit
    # code values. Canon's scene linear is reflection / 0.9, so its 20 %, 100 %, 800 % and 1600 % are
    # reflections 0.18, 0.90, 7.2 and 14.4.
    ("canon-log3", 0.0, _ten_bit(128), 0.5 * CV10),
    ("canon-log3", 0.18, _ten_bit(351), 0.5 * CV10),
    ("canon-log3", 0.90, _ten_bit(577), 0.5 * CV10),
    ("canon-log3", 7.2, _ten_bit(908), 0.5 * CV10),
    ("canon-log3", 14.4, _ten_bit(1020), 0.5 * CV10),
    # Academy S-2016-001, ACEScct: 18 % grey at 0.4136 (0.41358840 unrounded) (recalled).
    ("acescct", 0.18, 0.4136, 5e-5),
    # Blackmagic Design, DaVinci Resolve 17 Wide Gamut Intermediate, version 1.1 (2021-07-31), "Mapping
    # Values", page 4. Tested to one unit in the last printed place: the printed constants give 0.9031245
    # at 40.0, so Blackmagic's table seems to come from unrounded constants.
    ("davinci-intermediate", -0.01, -0.104443, 1e-6),
    ("davinci-intermediate", 0.0, 0.0, 1e-6),
    ("davinci-intermediate", 0.18, 0.336043, 1e-6),
    ("davinci-intermediate", 1.0, 0.513837, 1e-6),
    ("davinci-intermediate", 10.0, 0.756599, 1e-6),
    ("davinci-intermediate", 40.0, 0.903125, 1e-6),
    ("davinci-intermediate", 100.0, 1.0, 1e-6),
    # Apple Log Profile White Paper (2023-09), page 4, and Apple Log 2 White Paper (2025-09), page 4:
    # the same table, float signal to six places. Tested to one unit in the last printed place: the
    # printed constants give 0.1504765 at 0 and 0.6816868 at 0.9, so Apple's table seems to come from
    # unrounded constants.
    ("apple-log", 0.0, 0.150477, 1e-6),
    ("apple-log", 0.18, 0.488272, 1e-6),
    ("apple-log", 0.90, 0.681686, 1e-6),
    ("apple-log", 12.0, 1.0, 1e-6),
    ("apple-log", 0.0, _ten_bit(154), 0.5 * CV10),
    ("apple-log", 0.18, _ten_bit(500), 0.5 * CV10),
    ("apple-log", 0.90, _ten_bit(697), 0.5 * CV10),
    # Samsung Log White Paper (2025), table 1, page 6: float signal to six places and 10-bit full range.
    ("samsung-log", 0.0, 0.125124, 5e-7),
    ("samsung-log", 0.01, 0.206562, 5e-7),
    ("samsung-log", 0.18, 0.527859, 5e-7),
    ("samsung-log", 0.90, 0.708700, 5e-7),
    ("samsung-log", 12.0, 1.0, 5e-7),
    ("samsung-log", 0.0, _ten_bit(128), 0.5 * CV10),
    ("samsung-log", 0.01, _ten_bit(211), 0.5 * CV10),
    ("samsung-log", 0.18, _ten_bit(540), 0.5 * CV10),
    ("samsung-log", 0.90, _ten_bit(725), 0.5 * CV10),
    # Kodak, Version 2.1 (1995-07-26), sections 4.1 and 4.2, page 6: black 95, white 685.
    ("cineon", 0.0, _ten_bit(95), 1e-12),
    ("cineon", 1.0, _ten_bit(685), 1e-12),
    # ITU-R BT.2100-3 (02/2025), table 5, page 7: E = 1/12 gives 0.5, E = 1 gives 1.0.
    ("hlg", 1 / 12, 0.5, 1e-12),
    ("hlg", 1.0, 1.0, 5e-8),
    # ITU-R BT.2408-7: HDR reference white at 75 % HLG and 58 % PQ (203 cd/m2) (recalled).
    ("pq", 203.0, 0.58, 5e-3),
    # IEC 61966-2-1: the breakpoint 0.0031308 encodes to 0.04045, 1.0 to 1.0 (recalled).
    ("srgb", 0.0031308, 0.04045, 5e-6),
    ("srgb", 1.0, 1.0, 1e-12),
    # ITU-R BT.709-6 (06/2015), item 1.2, page 5: the breakpoint 0.018 encodes to 0.081.
    ("bt709", 0.018, 0.081, 5e-4),
    ("linear", 0.18, 0.18, 0.0),
]


@pytest.mark.parametrize(("transfer_id", "x", "expected", "tol"), PUBLISHED)
def test_published_reference_values(transfer_id: str, x: float, expected: float, tol: float) -> None:
    t = tf.TRANSFERS[transfer_id]
    assert float(t.encode(x)) == pytest.approx(expected, abs=tol)
    low, high = (float(v) for v in t.decode(np.array([expected - tol, expected + tol])))
    assert low - 1e-12 <= x <= high + 1e-12


def test_hlg_constants_follow_bt2100() -> None:
    # ITU-R BT.2100-3, table 5 and note 5c, pages 7 and 8: b and c printed to eight places.
    assert tf._HLG_A == 0.17883277
    assert abs(tf._HLG_B - 0.28466892) < 5e-9
    assert abs(tf._HLG_C - 0.55991073) < 5e-9


def test_pq_constants_follow_bt2100() -> None:
    # ITU-R BT.2100-3, table 4, page 6: the exact fractions and their printed decimals.
    assert (tf._M1, tf._M2) == (0.1593017578125, 78.84375)
    assert (tf._C1, tf._C2, tf._C3) == (0.8359375, 18.8515625, 18.6875)
    assert tf._C1 == tf._C3 - tf._C2 + 1


def test_hlg_reference_white_and_grey_follow_bt2408() -> None:
    white = tf.hlg_decode(0.75)
    assert float(white) == pytest.approx(0.2650, abs=5e-5)
    assert float(tf.hlg_encode(0.18 * white)) == pytest.approx(0.38, abs=5e-3)
    assert tf.TRANSFERS["hlg"].grey == pytest.approx(0.18 * white)


def test_pq_grey_follows_bt2408() -> None:
    assert float(tf.pq_encode(26.0)) == pytest.approx(0.38, abs=5e-3)


def test_cineon_follows_kodaks_density_convention() -> None:
    # Kodak, pages 4 and 7: 0.002 density per code value and a negative gamma of 0.6. One stop far
    # above black is 0.6 * log10(2) / 0.002 = 90.3 codes; the black offset shrinks the step slightly.
    step = float(tf.cineon_encode(64.0) - tf.cineon_encode(32.0)) * 1023
    assert step == pytest.approx(300 * np.log10(2), abs=0.05)


# ARRI, ALEXA Log C Curve Usage in VFX (2017-03-09), page 9: the exposure-value table, every row and
# every column (cut, a, b, c, d, e, f, e * cut + f) as printed.
ARRI_LOGC3_TABLE = {
    160: (0.005561, 5.555556, 0.080216, 0.269036, 0.381991, 5.842037, 0.092778, 0.125266),
    200: (0.006208, 5.555556, 0.076621, 0.266007, 0.382478, 5.776265, 0.092782, 0.128643),
    250: (0.006871, 5.555556, 0.072941, 0.262978, 0.382966, 5.710494, 0.092786, 0.132021),
    320: (0.007622, 5.555556, 0.068768, 0.259627, 0.383508, 5.637732, 0.092791, 0.135761),
    400: (0.008318, 5.555556, 0.064901, 0.256598, 0.383999, 5.571960, 0.092795, 0.139142),
    500: (0.009031, 5.555556, 0.060939, 0.253569, 0.384493, 5.506188, 0.092800, 0.142526),
    640: (0.009840, 5.555556, 0.056443, 0.250219, 0.385040, 5.433426, 0.092805, 0.146271),
    800: (0.010591, 5.555556, 0.052272, 0.247190, 0.385537, 5.367655, 0.092809, 0.149658),
    1000: (0.011361, 5.555556, 0.047996, 0.244161, 0.386036, 5.301883, 0.092814, 0.153047),
    1280: (0.012235, 5.555556, 0.043137, 0.240810, 0.386590, 5.229121, 0.092819, 0.156799),
    1600: (0.013047, 5.555556, 0.038625, 0.237781, 0.387093, 5.163350, 0.092824, 0.160192),
}


@pytest.mark.parametrize(("ei", "params"), sorted(ARRI_LOGC3_TABLE.items()))
def test_logc3_parameters_follow_arris_table(ei: int, params: tuple[float, ...]) -> None:
    assert tf.LOGC3_EI[ei] == params
    # ARRI's curves put 18 % grey at the same signal at every exposure index.
    assert float(tf.logc3_encode(0.18, ei=ei)) == pytest.approx(0.391007, abs=5e-6)


def test_logc3_rejects_an_unknown_exposure_index() -> None:
    with pytest.raises(ValueError, match="EI 900"):
        tf.logc3_encode(0.18, ei=900)


@pytest.mark.parametrize("ei", sorted(tf.LOGC3_EI))
def test_logc3_round_trips_at_every_exposure_index(ei: int) -> None:
    x = np.linspace(float(tf.logc3_decode(0.0, ei=ei)), float(tf.logc3_decode(1.0, ei=ei)), 2001)
    np.testing.assert_allclose(tf.logc3_decode(tf.logc3_encode(x, ei=ei), ei=ei), x, rtol=1e-9, atol=1e-12)


TRANSFERS = list(tf.TRANSFERS.values())

# Steps the makers' printed (rounded) constants leave at a breakpoint: (breakpoint, step in code value).
# BT.709's power segment starts 0.00025 above its linear one; F-Log's linear segment ends 0.0001 above
# the start of its log one, so a sliver of linear values just below the breakpoint encodes higher than
# the breakpoint itself and does not round-trip. Both are kept as printed (F-Log's e = 8.735631 is
# printed so in the F-Log Data Sheet Ver.1.2, page 2). Samsung Log's lower log segment ends 0.0000036
# above where its upper one starts at x_t = 0.01 (Samsung's own y_t is the upper segment's value).
KNOWN_STEPS = {
    "bt709": (0.018, 0.00024795),
    "f-log": (0.00089, -0.00010092),
    "samsung-log": (0.01, -0.0000036052),
}


def _outside_known_step(t: tf.Transfer, x: np.ndarray) -> np.ndarray:
    if t.id not in KNOWN_STEPS:
        return x
    cut, _ = KNOWN_STEPS[t.id]
    return x[np.abs(x - cut) > 0.02 * cut]


@pytest.mark.parametrize(("transfer_id", "step"), [(k, v[1]) for k, v in KNOWN_STEPS.items()])
def test_known_steps_are_the_makers_rounding(transfer_id: str, step: float) -> None:
    t = tf.TRANSFERS[transfer_id]
    cut = t.breaks[0]
    below, above = (float(v) for v in t.encode(np.array([cut - 1e-12, cut])))
    assert above - below == pytest.approx(step, abs=5e-8)


def _span(t: tf.Transfer) -> tuple[float, float]:
    """The linear values that code values 0 and 1 decode to: the curve's whole range, below black included."""
    return float(t.decode(0.0)), float(t.decode(1.0))


@pytest.mark.parametrize("t", TRANSFERS, ids=lambda t: t.id)
def test_round_trip_across_the_range(t: tf.Transfer) -> None:
    lo, hi = _span(t)
    x = np.concatenate([np.linspace(lo, hi, 4001), np.geomspace(1e-6, hi, 2001)])
    x = _outside_known_step(t, x[x >= lo])
    np.testing.assert_allclose(t.decode(t.encode(x)), x, rtol=1e-9, atol=1e-7 * max(1.0, hi))


@pytest.mark.parametrize("t", TRANSFERS, ids=lambda t: t.id)
def test_encode_is_monotonic(t: tf.Transfer) -> None:
    lo, hi = _span(t)
    x = _outside_known_step(t, np.linspace(lo, hi, 200001))
    assert np.all(np.diff(t.encode(x)) > 0)
    code = np.linspace(0.0, 1.0, 200001)
    if t.id in KNOWN_STEPS:
        cut_code = float(t.encode(KNOWN_STEPS[t.id][0]))
        code = code[np.abs(code - cut_code) > 0.001]
    assert np.all(np.diff(t.decode(code)) > 0)


@pytest.mark.parametrize(
    "t", [t for t in TRANSFERS if t.breaks and t.id not in KNOWN_STEPS], ids=lambda t: t.id
)
def test_continuous_at_breakpoints(t: tf.Transfer) -> None:
    for cut in t.breaks:
        eps = 1e-9 * max(1.0, abs(cut))
        below, above = (float(v) for v in t.encode(np.array([cut - eps, cut + eps])))
        assert above - below == pytest.approx(0.0, abs=2e-6)
        code = float(t.encode(cut))
        back = t.decode(np.array([code - 1e-9, code + 1e-9]))
        assert float(back[1] - back[0]) == pytest.approx(0.0, abs=2e-6 * max(1.0, abs(cut)))


@pytest.mark.parametrize("t", TRANSFERS, ids=lambda t: t.id)
def test_grey_code_is_the_encoded_grey(t: tf.Transfer) -> None:
    assert float(t.encode(t.grey)) == pytest.approx(t.grey_code, abs=5e-7)


def test_each_transfer_names_its_input_scale() -> None:
    scales = {t.id: t.scale for t in TRANSFERS}
    assert scales.pop("pq") == "nits"
    assert scales.pop("hlg") == "hlg-e"
    assert set(scales.values()) == {"scene"}
    assert all(t.grey == 0.18 for t in TRANSFERS if t.scale == "scene")


@pytest.mark.parametrize("transfer_id", ["pq", "hlg"])
def test_scene_linear_is_refused_by_curves_on_another_scale(transfer_id: str) -> None:
    t = tf.TRANSFERS[transfer_id]
    with pytest.raises(ValueError, match=t.scale):
        t.encode_scene(0.18)
    with pytest.raises(ValueError, match=t.scale):
        t.decode_scene(0.5)


def test_scene_linear_passes_through_scene_curves() -> None:
    t = tf.TRANSFERS["s-log3"]
    assert float(t.encode_scene(0.18)) == float(t.encode(0.18))
    assert float(t.decode_scene(0.5)) == float(t.decode(0.5))


@pytest.mark.parametrize("ei", sorted(tf.LOGC3_EI))
def test_logc3_exposure_index_is_reachable_through_the_registry(ei: int) -> None:
    t = tf.transfer("logc3", ei=ei)
    assert t.id == "logc3"
    assert t.name == "ARRI LogC3"
    assert f"EI {ei}" in t.reference
    x = np.array([0.0, 0.005, 0.18, 2.0])
    np.testing.assert_array_equal(t.encode(x), tf.logc3_encode(x, ei=ei))
    np.testing.assert_array_equal(t.decode(t.encode(x)), tf.logc3_decode(tf.logc3_encode(x, ei=ei), ei=ei))
    assert t.breaks == (tf.LOGC3_EI[ei][0],)
    assert t.grey_code == pytest.approx(0.391007, abs=5e-6)


def test_transfer_lookup() -> None:
    assert tf.transfer("s-log3") is tf.TRANSFERS["s-log3"]
    assert tf.transfer("logc3") is tf.TRANSFERS["logc3"]
    assert tf.transfer("logc3", ei=800).name == tf.TRANSFERS["logc3"].name
    with pytest.raises(KeyError, match="no-such-curve"):
        tf.transfer("no-such-curve")
    with pytest.raises(ValueError, match="EI 900"):
        tf.transfer("logc3", ei=900)
    with pytest.raises(ValueError, match="s-log3"):
        tf.transfer("s-log3", ei=800)


def test_every_transfer_has_a_name_and_a_reference() -> None:
    for transfer_id, t in tf.TRANSFERS.items():
        assert t.id == transfer_id
        assert t.name
        assert t.reference


def test_samsung_log_follows_samsungs_white_paper() -> None:
    # Samsung Log White Paper (2025), page 5: x_0 = -0.05, below which the signal holds at 0 and a
    # negative signal decodes to x_0; y_t = 0.206561909 is the signal at x_t = 0.01.
    assert float(tf.samsung_log_encode(-0.2)) == 0.0
    assert float(tf.samsung_log_decode(-0.1)) == -0.05
    assert float(tf.samsung_log_encode(0.01)) == pytest.approx(0.206561909, abs=5e-10)
    assert tf.TRANSFERS["samsung-log"].breaks == (0.01,)


def test_canon_log3_is_defined_below_black() -> None:
    x = np.array([-0.05, -0.02, -0.0141])
    np.testing.assert_allclose(tf.canon_log3_decode(tf.canon_log3_encode(x)), x, rtol=1e-9)
    assert np.all(np.diff(tf.canon_log3_encode(x)) > 0)


def test_apple_log_holds_at_zero_below_r0() -> None:
    assert float(tf.apple_log_encode(-0.2)) == 0.0
    assert float(tf.apple_log_decode(0.0)) == pytest.approx(-0.05641088)


@pytest.mark.parametrize(
    ("transfer_id", "label"),
    [
        ("cineon", "Cineon Log"),
        ("hlg", "REC.2020 HLG HDR"),
        ("pq", "ST2084 PQ HDR"),
        ("bt709", "BT.709"),
        ("srgb", "sRGB"),
        ("v-log", "Panasonic VLOG"),
        ("davinci-intermediate", "DaVinci Intermediate"),
        ("acescct", "ACES CCT"),
        ("canon-log3", "Canon Log 3"),
        ("s-log3", "Sony S-Log3"),
        ("apple-log", "Apple Log"),
        ("logc3", "ARRI LogC3"),
        ("samsung-log", "Samsung Log"),
        ("f-log", "F-Log"),
        ("linear", "Linear"),
        ("f-log2", "Fujifilm F-Log2"),
    ],
)
def test_transfer_labels_match_motioncam(transfer_id: str, label: str) -> None:
    assert tf.TRANSFERS[transfer_id].name == label


def test_cineon_matches_kodaks_linear_lut() -> None:
    # Kodak, Version 2.1 (1995-07-26), section 5.3, page 7: display gamma 1.7.
    # Normalise the 8-bit linear output to 0-1, without quantisation.
    code = np.array([95.0, 180.0, 300.0, 470.0, 600.0, 685.0])
    gain = 255 / (1 - 10 ** ((95 - 685) * 0.002 / 0.6))
    offset = gain - 255
    linear = (10 ** ((code - 685) * 0.002 / 0.6) * gain - offset) / 255
    np.testing.assert_allclose(tf.cineon_decode(code / 1023), linear, rtol=0, atol=1e-15)
    np.testing.assert_allclose(tf.cineon_encode(linear), code / 1023, rtol=0, atol=1e-14)
