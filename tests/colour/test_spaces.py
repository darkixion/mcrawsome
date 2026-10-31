import numpy as np
import pytest

from mcrawsome.colour import spaces as sp


def test_white_maps_to_white() -> None:
    m = sp.conversion(sp.AP1, sp.ACES_WHITE, sp.REC709, sp.D65)
    np.testing.assert_allclose(m @ np.ones(3), np.ones(3), atol=1e-4)


def test_ap1_to_rec709_matches_published_matrix() -> None:
    expected = np.array(
        [[1.70505, -0.62179, -0.08326], [-0.13026, 1.14080, -0.01055], [-0.02400, -0.12897, 1.15297]]
    )
    np.testing.assert_allclose(sp.AP1_TO_REC709, expected, atol=2e-3)


def test_luminance_weights_sum_to_one() -> None:
    assert abs(sp.AP1_LUMINANCE.sum() - 1.0) < 1e-6


# Published RGB to XYZ matrices, each with the number of decimal places its maker prints. Page numbers
# are the PDF's own pages. "Recalled" marks a matrix whose document is not among the owner's copies.
PUBLISHED_TO_XYZ = {
    # IEC 61966-2-1:1999, section 5.2 (recalled).
    "rec709": (4, [[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]),
    # Panasonic, V-Log/V-Gamut Reference Manual, Rev.1.0 (2014-11-28), section 4.2, page 6.
    "v-gamut": (
        6,
        [[0.679644, 0.152211, 0.118600], [0.260686, 0.774894, -0.035580], [-0.009310, -0.004612, 1.102980]],
    ),
    # ARRI, ALEXA Log C Curve Usage in VFX (2017-03-09), page 10.
    "awg3": (
        6,
        [[0.638008, 0.214704, 0.097744], [0.291954, 0.823841, -0.115795], [0.002798, -0.067034, 1.153294]],
    ),
    # Blackmagic Design, DaVinci Resolve 17 Wide Gamut Intermediate, version 1.1 (2021-07-31), page 3.
    "davinci-wg": (
        8,
        [
            [0.70062239, 0.14877482, 0.10105872],
            [0.27411851, 0.87363190, -0.14775041],
            [-0.09896291, -0.13789533, 1.32591599],
        ],
    ),
    # SMPTE ST 2065-1:2012, section 4 (recalled).
    "ap0": (
        10,
        [
            [0.9525523959, 0.0, 0.0000936786],
            [0.3439664498, 0.7281660966, -0.0721325464],
            [0.0, 0.0, 1.0088251844],
        ],
    ),
    # Academy TB-2014-004, ACEScg, section 4 (recalled).
    "ap1": (
        10,
        [
            [0.6624541811, 0.1340042065, 0.1561876870],
            [0.2722287168, 0.6740817658, 0.0536895174],
            [-0.0055746495, 0.0040607335, 1.0103391003],
        ],
    ),
    # Not printed in Sony's Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3, which gives
    # only the primaries (page 7): these two are recalled from Sony's ACES documentation.
    "s-gamut3": (
        10,
        [
            [0.7064827132, 0.1288010498, 0.1151721641],
            [0.2709796708, 0.7866064112, -0.0575860820],
            [-0.0096778454, 0.0046000375, 1.0941355587],
        ],
    ),
    "s-gamut3-cine": (
        10,
        [
            [0.5990839208, 0.2489255161, 0.1024464902],
            [0.2150758201, 0.8850685017, -0.1001443219],
            [-0.0320658495, -0.0276583907, 1.1487819910],
        ],
    ),
}

# Published XYZ to RGB matrices, from the same pages as above.
PUBLISHED_FROM_XYZ = {
    "v-gamut": (
        6,
        [[1.589012, -0.313204, -0.180965], [-0.534053, 1.396011, 0.102458], [0.011179, 0.003194, 0.905535]],
    ),
    "awg3": (
        6,
        [[1.789066, -0.482534, -0.200076], [-0.639849, 1.396400, 0.194432], [-0.041532, 0.082335, 0.878868]],
    ),
    "davinci-wg": (
        8,
        [
            [1.51667204, -0.28147805, -0.14696363],
            [-0.46491710, 1.25142378, 0.17488461],
            [0.06484905, 0.10913934, 0.76141462],
        ],
    ),
}

# Published linear RGB to Rec.709 matrices (same white point, so no adaptation).
PUBLISHED_TO_REC709 = {
    # Panasonic, V-Log/V-Gamut Reference Manual, Rev.1.0, section 4.2, page 6.
    "v-gamut": (
        6,
        [
            [1.806576, -0.695697, -0.110879],
            [-0.170090, 1.305955, -0.135865],
            [-0.025206, -0.154468, 1.179674],
        ],
    ),
    # ARRI, ALEXA Log C Curve Usage in VFX, "Linear Conversion", page 7 (one entry printed to 5 places).
    "awg3": (
        6,
        [[1.617523, -0.537287, -0.080237], [-0.070573, 1.334613, -0.26404], [-0.021102, -0.226954, 1.248056]],
    ),
}

MATRIX_GAMUTS = [g for g in sp.GAMUTS.values() if g.primaries is not None]


@pytest.mark.parametrize("gamut_id", sorted(PUBLISHED_TO_XYZ))
def test_to_xyz_matches_the_makers_matrix(gamut_id: str) -> None:
    places, expected = PUBLISHED_TO_XYZ[gamut_id]
    got = sp.GAMUTS[gamut_id].to_xyz()
    np.testing.assert_allclose(got, np.array(expected), rtol=0, atol=0.5 * 10.0**-places + 1e-12)


@pytest.mark.parametrize("gamut_id", sorted(PUBLISHED_FROM_XYZ))
def test_from_xyz_matches_the_makers_matrix(gamut_id: str) -> None:
    places, expected = PUBLISHED_FROM_XYZ[gamut_id]
    got = sp.GAMUTS[gamut_id].from_xyz()
    np.testing.assert_allclose(got, np.array(expected), rtol=0, atol=0.5 * 10.0**-places + 1e-12)


@pytest.mark.parametrize("gamut_id", sorted(PUBLISHED_TO_REC709))
def test_conversion_to_rec709_matches_the_makers_matrix(gamut_id: str) -> None:
    places, expected = PUBLISHED_TO_REC709[gamut_id]
    got = sp.gamut_conversion(gamut_id, "rec709")
    np.testing.assert_allclose(got, np.array(expected), rtol=0, atol=0.5 * 10.0**-places + 1e-12)


def test_rec709_to_rec2020_matches_bt2087() -> None:
    # ITU-R BT.2087-0, equation (4); printed to four places (recalled).
    expected = np.array([[0.6274, 0.3293, 0.0433], [0.0691, 0.9195, 0.0114], [0.0164, 0.0880, 0.8956]])
    got = sp.conversion(sp.REC709, sp.D65, sp.REC2020, sp.D65)
    np.testing.assert_allclose(got, expected, rtol=0, atol=5e-5)


@pytest.mark.parametrize("gamut", MATRIX_GAMUTS, ids=lambda g: g.id)
def test_gamut_round_trips_through_xyz(gamut: sp.Gamut) -> None:
    rgb = np.array([[0.18, 0.18, 0.18], [1.0, 0.0, 0.0], [0.02, 0.5, 3.0], [-0.1, 0.2, 0.4]])
    xyz = rgb @ gamut.to_xyz().T
    np.testing.assert_allclose(xyz @ gamut.from_xyz().T, rgb, atol=1e-12)


@pytest.mark.parametrize("gamut", MATRIX_GAMUTS, ids=lambda g: g.id)
def test_gamut_white_maps_to_its_white_point(gamut: sp.Gamut) -> None:
    assert gamut.white is not None
    np.testing.assert_allclose(gamut.to_xyz() @ np.ones(3), sp.xy_to_xyz(gamut.white), atol=1e-12)


@pytest.mark.parametrize("gamut", MATRIX_GAMUTS, ids=lambda g: g.id)
def test_gamut_white_stays_white_through_ap1(gamut: sp.Gamut) -> None:
    np.testing.assert_allclose(sp.gamut_conversion(gamut.id, "ap1") @ np.ones(3), np.ones(3), atol=1e-12)
    np.testing.assert_allclose(sp.gamut_conversion("ap1", gamut.id) @ np.ones(3), np.ones(3), atol=1e-12)


def test_gamut_primaries_are_the_published_ones() -> None:
    # Checked against: ITU-R BT.709-6 items 1.3 and 1.4 (page 5); ITU-R BT.2020-2 table 3 (page 5);
    # Panasonic page 5; ARRI page 10; Sony page 7 (S-Gamut3, S-Gamut3.Cine and ACES AP0); Samsung Log
    # White Paper (2025) table 2, page 7 (DCI-P3 primaries with D65, which is Display P3). Cinema Gamut
    # is recalled: Canon's Log white paper does not give it.
    g = sp.GAMUTS
    assert g["rec709"].primaries == ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060))
    assert g["rec2020"].primaries == ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046))
    assert sp.D65 == (0.3127, 0.3290)
    assert sp.ACES_WHITE == (0.32168, 0.33767)
    assert g["v-gamut"].primaries == ((0.730, 0.280), (0.165, 0.840), (0.100, -0.030))
    assert g["awg3"].primaries == ((0.6840, 0.3130), (0.2210, 0.8480), (0.0861, -0.1020))
    assert g["cinema-gamut"].primaries == ((0.7400, 0.2700), (0.1700, 1.1400), (0.0800, -0.1000))
    assert g["s-gamut3"].primaries == ((0.730, 0.280), (0.140, 0.855), (0.100, -0.050))
    assert g["s-gamut3-cine"].primaries == ((0.766, 0.275), (0.225, 0.800), (0.089, -0.087))
    assert g["display-p3"].primaries == ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060))
    assert g["ap0"].primaries == ((0.7347, 0.2653), (0.0, 1.0), (0.0001, -0.0770))
    assert g["ap0"].white == g["ap1"].white == sp.ACES_WHITE
    assert all(g[i].white == sp.D65 for i in g if i not in ("ap0", "ap1", "camera-native"))


def test_apple_wide_gamut_is_apples_printed_one() -> None:
    # Apple Log 2 White Paper (2025-09, version 1.1), "Apple Wide Gamut", page 6. Apple prints no
    # matrix, so the round-trip and white tests above are the only other checks.
    gamut = sp.GAMUTS["apple-wide-gamut"]
    assert gamut.name == "Apple Log 2"
    assert gamut.primaries == ((0.725, 0.301), (0.221, 0.814), (0.068, -0.076))
    assert gamut.white == sp.D65


def test_every_gamut_has_a_name_and_a_reference() -> None:
    for gamut_id, gamut in sp.GAMUTS.items():
        assert gamut.id == gamut_id
        assert gamut.name
        assert gamut.reference


def test_camera_native_has_no_matrix() -> None:
    native = sp.GAMUTS["camera-native"]
    assert native.primaries is None
    assert native.white is None
    with pytest.raises(ValueError, match="no matrix"):
        native.to_xyz()
    with pytest.raises(ValueError, match="no matrix"):
        sp.gamut_conversion("camera-native", "ap1")


def test_unknown_gamut_is_named_in_the_error() -> None:
    with pytest.raises(KeyError, match="no-such-gamut"):
        sp.gamut_conversion("no-such-gamut", "ap1")


@pytest.mark.parametrize(
    ("gamut_id", "label"),
    [
        ("rec709", "sRGB"),
        ("rec2020", "Rec.2020"),
        ("v-gamut", "Panasonic Wide Gamut"),
        ("awg3", "ARRI Wide Gamut 3"),
        ("davinci-wg", "DaVinci Wide Gamut"),
        ("ap1", "ACES AP1"),
        ("cinema-gamut", "Canon Cinema"),
        ("s-gamut3-cine", "Sony S-Gamut3 (Cinema)"),
        ("camera-native", "Camera Native"),
        ("apple-wide-gamut", "Apple Log 2"),
        ("display-p3", "Display P3"),
        ("s-gamut3", "Sony S-Gamut3"),
        ("ap0", "ACES AP0 (ACES2065-1)"),
    ],
)
def test_gamut_labels_match_motioncam(gamut_id: str, label: str) -> None:
    assert sp.GAMUTS[gamut_id].name == label


def test_rec709_can_show_both_motioncam_labels() -> None:
    assert sp.GAMUTS["rec709"].names == ("sRGB", "BT.709")
