from pathlib import Path

import pytest

from mcrawsome.colour import names
from mcrawsome.colour.spaces import GAMUTS
from mcrawsome.colour.transfer import TRANSFERS


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("250930_133630_VIDEO_25mm_Davinci_Davinci-WG.gcsv", ("davinci-wg", "davinci-intermediate")),
        ("261011_094111_VIDEO_25mm_BT.709_BT709.mp4", ("rec709", "bt709")),
        ("250930_133630_VIDEO_25mm_Davinci_Davinci-WG.mcraw", ("davinci-wg", "davinci-intermediate")),
        ("261011_094111_VIDEO_25mm_BT.709_BT709", ("rec709", "bt709")),
        ("261015_111726_VIDEO_25mm_HLG_sRGB.mp4", ("rec709", "hlg")),
        ("261015_111739_VIDEO_25mm_CINEON_BT709.mp4", ("rec709", "cineon")),
        ("261015_111747_VIDEO_25mm_PQ_Rec2020.mp4", ("rec2020", "pq")),
        ("261015_111759_VIDEO_25mm_BT.709_Panasonic-WG.mp4", ("v-gamut", "bt709")),
        ("261015_111807_VIDEO_25mm_sRGB_ARRI-WG3.mp4", ("awg3", "srgb")),
        ("261015_111814_VIDEO_25mm_Panasonic VLOG_Davinci-WG.mp4", ("davinci-wg", "v-log")),
        ("261015_111823_VIDEO_25mm_Davinci_ACES-AP1.mp4", ("ap1", "davinci-intermediate")),
        ("261015_111830_VIDEO_25mm_ACES CCT_Canon.mp4", ("cinema-gamut", "acescct")),
        ("261015_111838_VIDEO_25mm_Canon Log 3_S-Gamut3C.mp4", ("s-gamut3-cine", "canon-log3")),
        ("261015_111851_VIDEO_25mm_Sony S-Log3_Native.mp4", ("camera-native", "s-log3")),
        ("261015_111901_VIDEO_25mm_Apple Log_Apple-Log2.mp4", ("apple-wide-gamut", "apple-log")),
        ("261015_111908_VIDEO_25mm_LogC3_Display-P3.mp4", ("display-p3", "logc3")),
        ("261015_111913_VIDEO_25mm_Samsung Log_Display-P3.mp4", ("display-p3", "samsung-log")),
        ("261015_111918_VIDEO_25mm_F-Log_Display-P3.mp4", ("display-p3", "f-log")),
        ("261015_111923_VIDEO_25mm_Linear_Display-P3.mp4", ("display-p3", "linear")),
    ],
)
def test_reads_the_colour_tokens(name: str, expected: tuple[str, str]) -> None:
    assert names.colour_from_name(name) == expected


def test_reads_a_path() -> None:
    path = Path("/clips/2025/250930_133630_VIDEO_25mm_Davinci_Davinci-WG.gcsv")
    assert names.colour_from_name(path) == ("davinci-wg", "davinci-intermediate")


@pytest.mark.parametrize(
    "name",
    [
        "261004_174122_VIDEO_25mm.mcraw",
        "261010_135132_VIDEO_25mm.mcraw",
        "261010_131928_VIDEO_25mm.mcraw",
        "261010_154156_VIDEO_25mm.mcraw",
        "251011_153704_VIDEO_25mm.mcraw",
        "261006_005913_VIDEO_25mm.mcraw",
        "261004_174022_VIDEO_26mm.mcraw",
        "261004_174122_VIDEO_26mm.mcraw",
        "261008_095010_VIDEO_25mm.mcraw",
        "clip.mcraw",
        "261011_094111_VIDEO_25mm_BT.709.mp4",
        "261011_094111_VIDEO_25mm_SLog3_SGamut3.mp4",
        "261011_094111_VIDEO_25mm_Davinci_BT709_extra.mp4",
        "",
    ],
)
def test_names_without_recognised_tokens_give_none(name: str) -> None:
    assert names.colour_from_name(name) is None


def test_tables_name_registered_spaces() -> None:
    assert set(names.MOTIONCAM_GAMUT_TOKENS.values()) <= set(GAMUTS)
    assert set(names.MOTIONCAM_TRANSFER_TOKENS.values()) <= set(TRANSFERS)
