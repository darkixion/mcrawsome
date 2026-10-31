"""Colour encodings (a gamut and a transfer) and their conversions to and from scene-linear AP1."""

from typing import get_args

import numpy as np
import numpy.typing as npt
import pytest

from mcrawsome.colour import encoding as enc
from mcrawsome.colour.spaces import AP1_TO_DAVINCI_WG, AP1_TO_REC709, GAMUTS, gamut_conversion
from mcrawsome.colour.transfer import (
    TRANSFERS,
    acescct_decode,
    acescct_encode,
    bt709_oetf_inverse,
    davinci_intermediate_decode,
    davinci_intermediate_encode,
    hlg_decode,
    transfer,
)


def _frame(seed: int = 1) -> npt.NDArray[np.float32]:
    return np.random.default_rng(seed).uniform(0.0, 1.0, (16, 24, 3)).astype(np.float32)


def _old_to_linear_ap1(rgb: npt.NDArray[np.float32], key: str) -> npt.NDArray[np.float32]:
    """looks.cube.to_linear_ap1 as it was before part B."""
    if key == "acescct":
        return np.asarray(acescct_decode(rgb), dtype=np.float32)
    if key == "davinci":
        linear = np.asarray(davinci_intermediate_decode(rgb), dtype=np.float32)
        dwg_to_ap1 = np.linalg.inv(AP1_TO_DAVINCI_WG)
        return np.asarray(linear @ dwg_to_ap1.T.astype(np.float32), dtype=np.float32)
    linear = np.asarray(bt709_oetf_inverse(rgb), dtype=np.float32)
    rec709_to_ap1 = np.linalg.inv(AP1_TO_REC709)
    return np.asarray(linear @ rec709_to_ap1.T.astype(np.float32), dtype=np.float32)


def test_id_literals_match_the_registries() -> None:
    assert get_args(enc.GamutId) == tuple(GAMUTS)
    assert get_args(enc.TransferId) == tuple(TRANSFERS)
    assert set(get_args(enc.LogTransferId)) == {t for t, v in TRANSFERS.items() if v.scale == "scene"}


def test_legacy_keys_name_the_three_known_pairs() -> None:
    assert enc.encoding("davinci") == enc.ColourEncoding("davinci-wg", "davinci-intermediate")
    assert enc.encoding("bt709") == enc.ColourEncoding("rec709", "bt709")
    assert enc.encoding("acescct") == enc.ColourEncoding("ap1", "acescct")
    for key in ("davinci", "bt709", "acescct"):
        assert enc.encoding(key).key == key
    pair = enc.ColourEncoding("s-gamut3-cine", "s-log3")
    assert pair.key == "s-gamut3-cine/s-log3"
    assert enc.encoding(pair.key) == pair
    assert pair.name == "Sony S-Gamut3 (Cinema) / Sony S-Log3"


@pytest.mark.parametrize("key", ["", "davinci-wg", "nowhere/s-log3", "ap1/nothing", "ap1/s-log3/x"])
def test_unknown_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="colour encoding"):
        enc.encoding(key)


@pytest.mark.parametrize("key", ["acescct", "davinci", "bt709"])
def test_legacy_inputs_decode_exactly_as_before(key: str) -> None:
    rgb = _frame()
    assert np.array_equal(enc.to_linear_ap1(rgb, key), _old_to_linear_ap1(rgb, key))


def test_legacy_outputs_encode_exactly_as_before() -> None:
    linear = _frame(2) * np.float32(4.0)
    assert np.array_equal(enc.from_linear_ap1(linear, "acescct"), acescct_encode(linear))
    old = davinci_intermediate_encode(linear @ AP1_TO_DAVINCI_WG.T.astype(np.float32))
    assert np.array_equal(enc.from_linear_ap1(linear, "davinci"), old)


@pytest.mark.parametrize(
    "pair",
    [("s-gamut3-cine", "s-log3"), ("v-gamut", "v-log"), ("awg3", "logc3"), ("rec2020", "apple-log"),
     ("cinema-gamut", "canon-log3"), ("display-p3", "srgb"), ("ap1", "linear"), ("rec709", "cineon")],
)  # fmt: skip
def test_a_pair_puts_grey_at_its_grey_code_and_round_trips(pair: tuple[str, str]) -> None:
    gamut, curve = pair
    key = enc.ColourEncoding(gamut, curve).key
    code = np.full((1, 1, 3), transfer(curve).grey_code, np.float32)
    assert np.allclose(enc.to_linear_ap1(code, key), 0.18, atol=2e-5)  # grey is neutral in every gamut
    linear = np.float32(0.3) + _frame(3) * np.float32(0.4)  # inside every gamut, above every black
    back = enc.to_linear_ap1(np.asarray(enc.from_linear_ap1(linear, key), np.float32), key)
    assert np.allclose(back, linear, rtol=1e-4, atol=1e-5)


def test_a_wide_gamut_is_converted_with_its_matrix() -> None:
    code = np.asarray(transfer("s-log3").encode([[[0.5, 0.2, 0.1]]]), np.float32)
    expected = np.asarray([0.5, 0.2, 0.1]) @ gamut_conversion("s-gamut3-cine", "ap1").T
    assert np.allclose(enc.to_linear_ap1(code, "s-gamut3-cine/s-log3")[0, 0], expected, rtol=1e-5)


def test_hdr_inputs_put_diffuse_white_at_one() -> None:
    pq_white = np.full((1, 1, 3), transfer("pq").encode(203.0), np.float32)
    assert np.allclose(enc.to_linear_ap1(pq_white, "rec2020/pq"), 1.0, atol=1e-4)
    hlg_white = np.full((1, 1, 3), 0.75, np.float32)
    assert np.allclose(enc.to_linear_ap1(hlg_white, "rec2020/hlg"), 1.0, atol=1e-4)
    assert pytest.approx(float(hlg_decode(0.75))) == enc.HLG_REFERENCE_WHITE
    assert pytest.approx(0.26496, abs=1e-5) == enc.HLG_REFERENCE_WHITE


def test_hdr_curves_are_not_log_outputs() -> None:
    with pytest.raises(ValueError, match="not scene linear"):
        enc.from_linear_ap1(_frame(), "rec2020/hlg")


def test_camera_native_input_is_taken_as_ap1() -> None:
    code = _frame(4)
    native = enc.to_linear_ap1(code, "camera-native/s-log3")
    assert np.array_equal(native, enc.to_linear_ap1(code, "ap1/s-log3"))


def test_camera_native_output_uses_the_given_matrix() -> None:
    linear = _frame(5)
    native = np.asarray([[0.9, 0.1, 0.0], [0.05, 0.9, 0.05], [0.0, 0.2, 0.8]])
    out = enc.from_linear_ap1(linear, "camera-native/s-log3", native)
    expected = transfer("s-log3").encode(linear @ native.T.astype(np.float32))
    assert np.allclose(out, expected, atol=1e-6)
    passthrough = enc.from_linear_ap1(linear, "camera-native/s-log3")
    assert np.allclose(passthrough, transfer("s-log3").encode(linear), atol=1e-6)


@pytest.mark.parametrize(
    ("curve", "gamut"),
    [("davinci", "davinci-wg"), ("davinci-intermediate", "davinci-wg"), ("acescct", "ap1"), ("linear", "ap1"),
     ("bt709", "rec709"), ("srgb", "rec709"), ("cineon", "rec709"), ("v-log", "v-gamut"), ("logc3", "awg3"),
     ("s-log3", "s-gamut3-cine"), ("canon-log3", "cinema-gamut"), ("apple-log", "rec2020"),
     ("samsung-log", "rec2020"), ("f-log", "rec2020"), ("f-log2", "rec2020")],
)  # fmt: skip
def test_log_auto_gamut_is_the_curves_own(curve: str, gamut: str) -> None:
    chosen = enc.log_encoding(curve)
    assert chosen.gamut == gamut
    assert chosen.transfer == ("davinci-intermediate" if curve == "davinci" else curve)
    assert enc.log_encoding(curve, "rec2020").gamut == "rec2020"


def test_every_log_transfer_has_an_auto_gamut() -> None:
    assert set(enc.LOG_GAMUTS) == set(get_args(enc.LogTransferId))
    assert enc.log_encoding("davinci").key == "davinci"
    assert enc.log_encoding("acescct").key == "acescct"


@pytest.mark.parametrize("gamut", [g for g in GAMUTS if g != "camera-native"])
@pytest.mark.parametrize("curve", list(enc.LOG_GAMUTS))
def test_every_scene_pair_matches_the_verified_registry(gamut: str, curve: str) -> None:
    pair = enc.ColourEncoding(gamut, curve)
    rgb = np.asarray([[[0.12, 0.18, 0.25], [0.6, 0.4, 0.3]]], np.float32)
    matrix = gamut_conversion("ap1", gamut)
    expected = transfer(curve).encode_scene(rgb.astype(np.float64) @ matrix.T)
    np.testing.assert_allclose(enc.from_linear_ap1(rgb, pair.key), expected, rtol=2e-5, atol=2e-6)
    codes = np.asarray(transfer(curve).encode_scene(rgb), np.float32)
    decoded = transfer(curve).decode_scene(codes) @ gamut_conversion(gamut, "ap1").T
    np.testing.assert_allclose(enc.to_linear_ap1(codes, pair.key), decoded, rtol=2e-5, atol=2e-6)


# ---- ARRI LogC3 exposure index (Ruling 4, revised) ----


def test_logc3_ei_800_keeps_the_key_and_others_name_their_ei() -> None:
    assert enc.ColourEncoding("awg3", "logc3").key == "awg3/logc3"
    assert enc.ColourEncoding("awg3", "logc3", 800).key == "awg3/logc3"
    pair = enc.ColourEncoding("awg3", "logc3", 1600)
    assert pair.key == "awg3/logc3@1600"
    assert enc.encoding(pair.key) == pair
    assert pair.name == "ARRI Wide Gamut 3 / ARRI LogC3 (EI 1600)"


@pytest.mark.parametrize("key", ["awg3/logc3@900", "awg3/s-log3@800", "awg3/logc3@", "awg3/logc3@x"])
def test_bad_exposure_indexes_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match=r"colour encoding|EI|exposure index"):
        enc.encoding(key)


def test_logc3_conversions_use_the_exposure_index() -> None:
    code = _frame(7)
    linear = enc.to_linear_ap1(code, "awg3/logc3@1600")
    expected = np.asarray(transfer("logc3", ei=1600).decode_scene(code), np.float32)
    expected = expected @ gamut_conversion("awg3", "ap1").astype(np.float32).T
    np.testing.assert_allclose(linear, expected, rtol=1e-5, atol=1e-6)
    assert not np.allclose(linear, enc.to_linear_ap1(code, "awg3/logc3"))
    np.testing.assert_allclose(enc.from_linear_ap1(linear, "awg3/logc3@1600"), code, atol=2e-5)
    np.testing.assert_allclose(enc.decode_ap1(code, "awg3/logc3@1600"), linear, rtol=1e-4, atol=1e-5)


def test_log_encoding_takes_an_exposure_index() -> None:
    assert enc.log_encoding("logc3", "auto", 400) == enc.ColourEncoding("awg3", "logc3", 400)
    assert enc.log_encoding("logc3") == enc.ColourEncoding("awg3", "logc3")
