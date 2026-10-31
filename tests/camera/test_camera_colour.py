import json
from pathlib import Path

import numpy as np

from mcrawsome.camera.colour import camera_to_ap1
from mcrawsome.io.metadata import parse_container_metadata

META = parse_container_metadata(
    json.loads((Path(__file__).parent.parent / "data" / "container_metadata_pixel10.json").read_text())
)


def test_camera_white_maps_to_ap1_white() -> None:
    m, _ = camera_to_ap1(META, (0.51, 1.0, 0.65))
    np.testing.assert_allclose(m @ np.ones(3), np.ones(3), atol=1e-4)


def test_cct_is_plausible_for_daylight_and_tungsten() -> None:
    _, daylight = camera_to_ap1(META, (0.51, 1.0, 0.65))
    _, tungsten = camera_to_ap1(META, (0.82, 1.0, 0.35))
    assert 4000 < daylight < 7000
    assert 2000 < tungsten < 3500
    assert tungsten < daylight
