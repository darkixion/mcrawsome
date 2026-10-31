"""Optional real-recording regressions; media stays in the user's local sample folder."""

import os
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.camera.remosaic import detect_quad_bayer, remosaic
from mcrawsome.io.clip import Clip
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import reduce_cfa
from mcrawsome.recipe.raw import RawCalibration


@pytest.mark.local
@pytest.mark.parametrize("crop", ["none", "3840x2112"])
@pytest.mark.parametrize(("proxy", "factor"), [("full", 1), ("half", 2), ("quarter", 4), ("eighth", 8)])
def test_real_unflagged_quad_retains_single_remosaic_levels_and_proxy_pairing(
    proxy: str, factor: int, crop: str
) -> None:
    folder = Path(os.environ.get("MCRAWSOME_LOCAL_SAMPLES", Path.home() / "Downloads" / "motioncam"))
    path = folder / "261027_074803_VIDEO_25mm.mcraw"
    if not path.is_file():
        pytest.skip("optional real Quad Bayer recording unavailable")
    with (
        Clip(path, quad_bayer_remosaic=False) as recorded,
        Clip(path, quad_bayer_remosaic=True) as interpreted,
    ):
        raw = recorded.raw_frame(0)
        assert not raw.metadata.need_remosaic
        assert raw.metadata.is_binned is False
        assert detect_quad_bayer(raw.data, recorded.metadata.sensor_arrangement, raw.metadata.black_level)
        once = remosaic(raw.data, recorded.metadata.sensor_arrangement, raw.metadata.black_level)
        np.testing.assert_array_equal(interpreted.raw_frame(0).data, once)
        for policy in ("recorded", "static"):
            settings = RawCalibration.model_validate(
                {
                    "mount_proxy": proxy,
                    "mount_crop": crop,
                    "level_policy": policy,
                    "black_level": 900,
                    "white_level": 950,
                    "mount_camera_model": "Panasonic Varicam RAW",
                    "camera_model_source": "recorded",
                }
            )
            with Sequence(path, raw_settings=settings, quad_bayer_remosaic=True) as sequence:
                info = sequence.frame_info(0)
                assert info.black_levels == raw.metadata.black_level
                assert info.white_level == raw.metadata.white_level
                assert info.model != "Varicam RAW"
                assert info.cfa == recorded.metadata.sensor_arrangement
                assert (info.width, info.height) == (
                    raw.metadata.width // factor,
                    (2112 if crop != "none" else raw.metadata.height) // factor,
                )
                assert info.as_shot_neutral == raw.metadata.as_shot_neutral
                name = sequence.names()[0]
                size = sequence.size(name)
                pixels = info.width * info.height * 2
                output = np.frombuffer(
                    b"".join(
                        sequence.read(name, at, min(1 << 20, size - at))
                        for at in range(size - pixels, size, 1 << 20)
                    ),
                    "<u2",
                ).reshape(info.height, info.width)
                cropped = once[:2112] if crop != "none" else once
                expected = (
                    cropped
                    if factor == 1
                    else reduce_cfa(cropped, recorded.metadata.sensor_arrangement, factor)
                )
                np.testing.assert_array_equal(output, expected)
