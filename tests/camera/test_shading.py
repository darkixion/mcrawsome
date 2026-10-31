import numpy as np

from mcrawsome.camera.shading import lens_map, shading_gains
from mcrawsome.io.metadata import FrameMetadata


def _frame(map_values: list[list[float]]) -> FrameMetadata:
    return FrameMetadata.model_validate(
        {
            "width": 8,
            "height": 4,
            "originalWidth": 8,
            "originalHeight": 4,
            "orientation": 2,
            "compressionType": 7,
            "asShotNeutral": [0.5, 1, 0.6],
            "dynamicBlackLevel": [64] * 4,
            "dynamicWhiteLevel": 1023,
            "iso": 50,
            "exposureTime": 1e7,
            "lensShadingMap": map_values,
            "lensShadingMapWidth": 2,
            "lensShadingMapHeight": 2,
            "recvdTimestampMs": 0,
        }
    )


def test_constant_map_gives_constant_planes() -> None:
    planes = shading_gains(_frame([[1.5] * 4, [1.0] * 4, [2.0] * 4, [1.25] * 4]))
    assert [p.shape for p in planes] == [(2, 4)] * 4
    np.testing.assert_allclose([float(p.mean()) for p in planes], [1.5, 1.0, 2.0, 1.25])


CORNER = [
    [1.0, 2.0, 1.0, 3.0],  # R: a strong corner
    [1.0, 1.5, 1.0, 2.0],  # G on R rows
    [1.0, 1.5, 1.0, 2.0],  # G on B rows
    [1.0, 1.8, 1.0, 2.2],  # B
]


def test_colour_mode_keeps_the_ratios_with_no_gain_above_one() -> None:
    frame = _frame(CORNER)
    full, colour = lens_map(frame), lens_map(frame, "colour")
    assert colour.shape == full.shape == (4, 2, 2)
    assert float(colour.max()) <= 1.0 + 1e-6
    np.testing.assert_allclose(colour / colour[1], full / full[1], rtol=1e-6)
    np.testing.assert_allclose(colour[:, 0, 0], 1.0)  # where the four planes are equal, nothing changes
    np.testing.assert_allclose(colour[:, 1, 1], np.array([3.0, 2.0, 2.0, 2.2]) / 3.0, rtol=1e-6)


def test_the_full_mode_is_the_recorded_map() -> None:
    frame = _frame(CORNER)
    np.testing.assert_array_equal(lens_map(frame), np.array(CORNER, np.float32).reshape(4, 2, 2))
    for default, full in zip(shading_gains(frame), shading_gains(frame, "full"), strict=True):
        np.testing.assert_array_equal(default, full)


def test_colour_mode_shading_gains_never_brighten() -> None:
    planes = shading_gains(_frame(CORNER), "colour")
    assert max(float(p.max()) for p in planes) <= 1.0 + 1e-6


def test_vignette_only_gives_every_channel_the_green_falloff() -> None:
    frame = _frame([[2.0, 1.0, 1.2, 1.0], [1.8, 1.0, 1.1, 1.0], [1.6, 1.0, 1.1, 1.0], [2.4, 1.0, 1.3, 1.0]])
    full, vignette = lens_map(frame, "full"), lens_map(frame, "vignette")
    green = (full[1] + full[2]) / 2
    for channel in range(4):
        np.testing.assert_allclose(vignette[channel], green, rtol=1e-6)
    assert float(vignette.max()) > 1  # the falloff is still corrected
