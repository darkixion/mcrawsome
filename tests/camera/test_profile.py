from collections.abc import Iterator
from pathlib import Path

import pytest

from mcrawsome.camera import profile as profiles
from mcrawsome.camera.lens import LensCalibration
from mcrawsome.camera.profile import (
    CameraProfile,
    ProfileError,
    delete_profile,
    find_profile,
    focal_from_metadata,
    load_profile,
    save_profile,
)


@pytest.fixture
def user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profiles.all_profiles.cache_clear()
    folder = tmp_path / "mcrawsome" / "cameras"
    folder.mkdir(parents=True)
    yield folder
    profiles.all_profiles.cache_clear()


def test_focal_length_from_the_35mm_equivalent() -> None:
    assert focal_from_metadata(25.37, 4080, 3072) == pytest.approx(2990, rel=0.01)


def test_builtin_profile_for_the_pixel_main_camera() -> None:
    found = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 24.0)
    assert found is not None
    assert found.readout_ms == pytest.approx(18.0)
    assert found.gcsv_axes is not None
    assert found.gcsv_axes.signs == (1, -1, 1)


def test_builtin_profile_knows_direct_log_gyro_axes_and_sync() -> None:
    # measured on both Direct Log samples (2026-10-11): the same axes, sync -8 and -13.5 ms
    profile = find_profile("pixel 10 pro xl", None, 3840, 2160, 24.0)
    assert profile is not None
    assert profile.gpmf_axes is not None
    assert profile.gpmf_axes.axes == (2, 0, 1)
    assert profile.gpmf_axes.signs == (1, -1, 1)
    assert profile.gpmf_sync_ms == pytest.approx(-11.0)


def test_no_profile_for_an_unknown_phone() -> None:
    assert find_profile("some phone", None, 1920, 1080, 30.0) is None


def test_the_most_specific_profile_wins(user_dir: Path) -> None:
    match = {"device_model": "pixel 10 pro xl", "camera_id": "0/2", "width": 3840, "height": 2160}
    save_profile(CameraProfile(name="mine", match=match, readout_ms=12.0))
    found = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 24.0)
    assert found is not None
    assert found.name == "mine"
    other = find_profile("pixel 10 pro xl", "0/3", 3840, 2160, 24.0)
    assert other is not None
    assert other.name == "pixel_10_pro_xl_4k"


def test_a_user_profile_beats_an_equally_specific_builtin(user_dir: Path) -> None:
    match = {"device_model": "pixel 10 pro xl", "width": 3840, "height": 2160}
    save_profile(CameraProfile(name="mine", match=match, readout_ms=12.0))
    found = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 24.0)
    assert found is not None
    assert found.name == "mine"


def test_save_refuses_to_overwrite(user_dir: Path) -> None:
    p = CameraProfile(name="mine", match={"device_model": "x"}, readout_ms=10.0)
    save_profile(p)
    with pytest.raises(ProfileError, match="already exists"):
        save_profile(p)
    save_profile(p, force=True)


@pytest.mark.parametrize("model", ["Pixel 10 Pro XL", "  PIXEL 10 PRO XL  "])
def test_device_names_match_without_case_or_surrounding_whitespace(model: str, user_dir: Path) -> None:
    save_profile(CameraProfile(name="mixed_case", match={"device_model": model}, readout_ms=9))
    found = find_profile("pixel 10 pro xl", None, 1920, 1080, 24)
    assert found is not None
    assert found.name == "mixed_case"
    assert find_profile("Pixel 10 Pro", None, 1920, 1080, 24) is None


def test_lens_profile_keeps_matching_gyro_defaults_and_its_own_measurements(user_dir: Path) -> None:
    lens = LensCalibration(
        model="opencv_standard",
        width=3840,
        height=2160,
        original_width=3840,
        original_height=2160,
        camera_matrix=((3527, 0, 1920), (0, 3527, 1080), (0, 0, 1)),
        coefficients=(0.1, -0.2, 0, 0, 0.1),
        image_count=55,
        rms_error=1.1,
    )
    measured = CameraProfile(
        name="pixel10_lens_24fps",
        description="Measured screen target",
        match={"device_model": "Pixel 10 Pro XL", "width": 3840, "height": 2160, "fps": 24},
        focal_px=3527,
        lens_calibration=lens,
    )
    save_profile(measured)
    found = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 24.0015)
    assert found is not None
    assert found.name == measured.name
    assert found.focal_px == 3527
    assert found.lens_calibration == lens
    assert found.readout_ms == 18
    assert found.gcsv_axes is not None
    assert found.gpmf_axes is not None
    assert found.gcsv_sync_ms == -105
    assert found.gpmf_sync_ms == -11
    assert found.gcsv_gravity_axes is not None
    assert found.gcsv_gravity_camera_ids == ("0/2", "0/9")
    assert "gyro defaults from pixel_10_pro_xl_4k" in found.description
    # Explicit user values, including zero, take precedence over inherited defaults.
    save_profile(measured.model_copy(update={"readout_ms": 0, "gcsv_sync_ms": 0}), force=True)
    found = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 24)
    assert found is not None
    assert found.readout_ms == 0
    assert found.gcsv_sync_ms == 0
    # Neither FPS nor geometry matching is relaxed by case-insensitive device names.
    other = find_profile("pixel 10 pro xl", "0/2", 3840, 2160, 30)
    assert other is not None
    assert other.name == "pixel_10_pro_xl_4k"
    resized = find_profile("pixel 10 pro xl", "0/2", 1920, 1080, 24)
    assert resized is not None
    assert resized.gcsv_gravity_axes is not None
    assert resized.focal_px is None
    assert resized.lens_calibration is None
    assert resized.readout_ms is None
    assert resized.gcsv_sync_ms is None
    # Resolution/FPS-compatible profiles from another module must not supply gyro values.
    save_profile(
        measured.model_copy(
            update={
                "match": profiles.Match(
                    device_model="Another camera", camera_id="main", width=3840, height=2160, fps=24
                ),
            }
        ),
        force=True,
    )
    save_profile(
        CameraProfile(
            name="wrong_module", match={"device_model": "another camera", "camera_id": "wide"}, readout_ms=7
        )
    )
    save_profile(
        CameraProfile(name="wrong_fps", match={"device_model": "another camera", "fps": 30}, readout_ms=8)
    )
    found = find_profile("Another camera", "main", 3840, 2160, 24)
    assert found is not None
    assert found.readout_ms is None
    assert found.gcsv_gravity_axes is None
    assert load_profile(user_dir / "pixel10_lens_24fps.toml").gcsv_axes is None


def test_bad_profile_file_names_file_and_key(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('name = "bad"\nreadout_ms = -3\n[match]\ndevice_model = "x"\n')
    with pytest.raises(ProfileError, match=r"bad\.toml.*readout_ms"):
        load_profile(path)


@pytest.mark.parametrize("name", ["../outside", "bad-name", "", "/tmp/outside"])
def test_delete_rejects_names_that_are_not_profile_names(user_dir: Path, name: str) -> None:
    outside = user_dir.parent / "outside.toml"
    outside.write_text("keep this file")
    with pytest.raises(ProfileError, match="Invalid camera profile name"):
        delete_profile(name)
    assert outside.read_text() == "keep this file"


@pytest.mark.parametrize("size", [(4080, 3072), (1920, 1080), (1280, 720), (3072, 4080)])
@pytest.mark.parametrize("fps", [24.0015, 30, 60, 120])
@pytest.mark.parametrize("camera_id", ["0/2", "0/9"])
def test_verified_physical_axes_are_independent_of_recording_mode(
    size: tuple[int, int], fps: float, camera_id: str, user_dir: Path
) -> None:
    source = find_profile("pixel 10 pro xl", camera_id, 3840, 2160, 24)
    found = find_profile("  Pixel 10 Pro XL  ", camera_id, *size, fps)
    assert source is not None
    assert found is not None
    assert found.gcsv_gravity_axes == source.gcsv_gravity_axes
    assert found.gcsv_axes == source.gcsv_axes
    assert found.gcsv_gravity_camera_ids == (camera_id,)
    # Orientation is reusable; unrelated pixel geometry and sensor timing are not.
    assert found.focal_px is None
    assert found.lens_calibration is None
    assert found.readout_ms is None
    assert found.gcsv_sync_ms is None
    assert found.gpmf_sync_ms is None


@pytest.mark.parametrize("camera_id", [None, "0/3", "front"])
def test_cross_mode_axes_require_a_verified_camera_module(camera_id: str | None, user_dir: Path) -> None:
    assert find_profile("pixel 10 pro xl", camera_id, 4080, 3072, 24) is None
    assert find_profile("another phone", "0/2", 4080, 3072, 24) is None


def test_cross_mode_axes_fill_an_exact_profile_without_overwriting_it(user_dir: Path) -> None:
    save_profile(
        CameraProfile(
            name="mode",
            match={
                "device_model": "pixel 10 pro xl",
                "camera_id": "0/2",
                "width": 4080,
                "height": 3072,
                "fps": 24,
            },
            focal_px=2990,
            readout_ms=12,
            gcsv_sync_ms=0,
        )
    )
    found = find_profile("pixel 10 pro xl", "0/2", 4080, 3072, 24)
    assert found is not None
    assert found.name == "mode"
    assert found.focal_px == 2990
    assert found.readout_ms == 12
    assert found.gcsv_sync_ms == 0
    assert found.gcsv_gravity_axes is not None


def test_conflicting_cross_mode_gravity_measurements_are_not_guessed(user_dir: Path) -> None:
    from mcrawsome.camera.profile import Axes

    for name, width, signs in [("first", 1920, (1, 1, 1)), ("second", 3840, (-1, -1, 1))]:
        save_profile(
            CameraProfile(
                name=name,
                match={"device_model": "test phone", "camera_id": "main", "width": width},
                gcsv_gravity_axes=Axes(axes=(0, 1, 2), signs=signs),
            )
        )
    assert find_profile("test phone", "main", 4080, 3072, 60) is None
