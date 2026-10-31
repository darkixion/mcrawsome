from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcrawsome.camera.vignette import VignetteProfile, apply_profile, capture_profile
from mcrawsome.io.clip import Clip
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import parse_gain_maps, read_dng_tags
from mcrawsome.pipeline.render import FrameJob, first_metadata


def test_profile_round_trip_changes_metadata_without_decoding(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_mcraw(tmp_path / "tiny.mcraw", [0, 41_666_667, 83_333_334])
    with Clip(path) as clip:
        source = clip.frame_metadata(0)
        profile = capture_profile(clip.metadata, source)
        profile = profile.model_copy(
            update={"gains": tuple(tuple(2.0 for _ in row) for row in profile.gains)}
        )
        assert VignetteProfile.model_validate_json(profile.model_dump_json()) == profile

        def no_decode(*args: object) -> None:
            raise AssertionError("profile operations must not decode RAW")

        monkeypatch.setattr(clip, "raw_frame", no_decode)
        job = FrameJob(path, 0.0, (1.0, 1.0, 1.0), vignette_profile=profile)
        changed = first_metadata(job, clip)
        assert changed.lens_shading_map == [list(row) for row in profile.gains]
        assert clip.frame_metadata(0) == source
        wrong = profile.model_copy(update={"sensor_width": profile.sensor_width + 2})
        with pytest.raises(ValueError, match="sensor"):
            apply_profile(clip.metadata, source, wrong)
    with Sequence(path, vignette_profile=profile) as sequence:
        header, _ = sequence._header(0)
    dng = tmp_path / "header.dng"
    dng.write_bytes(header)
    maps = parse_gain_maps(bytes(read_dng_tags(dng)[51009]))
    assert all(float(gain.gains.min()) == 2.0 for gain in maps)


def test_profiles_refuse_invalid_maps() -> None:
    values = {
        "version": 1,
        "sensor_width": 128,
        "sensor_height": 64,
        "cfa": "rggb",
        "map_width": 2,
        "map_height": 2,
    }
    for gains in ([[1.0] * 4] * 3, [[1.0, 1.0, 1.0, float("nan")]] * 4, [[0.0] * 4] * 4):
        with pytest.raises(ValidationError):
            VignetteProfile.model_validate(values | {"gains": gains})


def test_preview_profile_change_redevelops_and_clear_restores(
    make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    import numpy as np

    from mcrawsome.output.export import frame_job, picture_key, read_clip_facts
    from mcrawsome.preview.session import PreviewSession, PreviewSettings
    from mcrawsome.recipe.load import build_recipe

    path = make_mcraw(tmp_path / "tiny.mcraw", [0, 41_666_667, 83_333_334])
    with Clip(path) as clip:
        profile = capture_profile(clip.metadata, clip.frame_metadata(0)).model_dump(mode="json")
    profile["gains"] = [[2.0] * (profile["map_width"] * profile["map_height"])] * 4
    plain = build_recipe([{"develop": {"exposure": 0}}])
    changed = build_recipe([{"develop": {"exposure": 0, "vignette_profile": profile}}])
    with PreviewSession(path, plain, PreviewSettings(gpu=False)) as session:
        before = session.frame(0)
        assert session.update(changed) != before
        with PreviewSession(path, changed, PreviewSettings(gpu=False)) as fresh:
            fresh.frame(0)
            np.testing.assert_array_equal(session.linear16()[0], fresh.linear16()[0])
        assert session.update(plain) == before
    facts = read_clip_facts(path, plain)
    assert picture_key(frame_job(path, plain, facts)) != picture_key(frame_job(path, changed, facts))
