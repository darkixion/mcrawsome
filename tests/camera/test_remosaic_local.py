"""Every frame of the optional real Quad Bayer sample matches the pre-optimisation algorithm."""

import os
from pathlib import Path

import numpy as np
import pytest
from remosaic_reference import remosaic as reference_remosaic

from mcrawsome.camera.remosaic import remosaic
from mcrawsome.io.clip import Clip


@pytest.mark.local
def test_real_quad_remosaic_is_exact_to_original_numpy() -> None:
    folder = Path(os.environ.get("MCRAWSOME_LOCAL_SAMPLES", Path.home() / "Downloads" / "motioncam"))
    path = folder / "261027_074803_VIDEO_25mm.mcraw"
    if not path.is_file():
        pytest.skip("optional real Quad Bayer recording unavailable")
    with Clip(path, quad_bayer_remosaic=False) as clip:
        for index in range(clip.frame_count):
            frame = clip.raw_frame(index)
            expected = reference_remosaic(
                frame.data, clip.metadata.sensor_arrangement, frame.metadata.black_level
            )
            actual = remosaic(frame.data, clip.metadata.sensor_arrangement, frame.metadata.black_level)
            np.testing.assert_array_equal(actual, expected, err_msg=f"frame {index}")
