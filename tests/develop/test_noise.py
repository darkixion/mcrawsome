import numpy as np


def test_recorded_noise_model_tracks_white_balance_shading_and_bad_profiles(tiny_raw: object) -> None:
    from pathlib import Path

    from mcrawsome.develop.noise import recorded_noise_model
    from mcrawsome.develop.raw import Developer
    from mcrawsome.io.clip import Clip

    assert isinstance(tiny_raw, Path)
    with Clip(tiny_raw) as clip:
        frame = clip.frame_metadata(0)
        plain = Developer(clip.metadata, frame, (1, 1, 1), lens_shading=0)
        bright = Developer(clip.metadata, frame, (0.5, 0.5, 0.5), lens_shading=0)
        profile = [0.001, 0.00001] * 4
        a = recorded_noise_model(plain, profile)
        b = recorded_noise_model(bright, profile)
        assert a is not None
        assert b is not None
        np.testing.assert_allclose(b.scale, np.asarray(a.scale) * 2, rtol=1e-5)
        np.testing.assert_allclose(b.offset, np.asarray(a.offset) * 4, rtol=1e-5)
        assert np.asarray(a.scale).shape == (64, 128)
        for invalid in ([], [1], [-1, 1] * 4, [float("nan"), 1] * 4, [float("inf"), 1] * 4):
            assert recorded_noise_model(plain, invalid) is None
