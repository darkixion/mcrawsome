from pathlib import Path

from mcrawsome.camera.profile import CameraProfile, all_profiles, delete_profile, save_profile
from mcrawsome.output.export import motion_key
from mcrawsome.recipe.schema import Recipe


def test_motion_cache_changes_when_a_profile_is_deleted_or_replaced(tmp_path: Path) -> None:
    all_profiles.cache_clear()
    clip = tmp_path / "clip.mcraw"
    clip.write_bytes(b"unchanged source fingerprint")
    recipe = Recipe()
    profile = CameraProfile(name="replacement", focal_px=1000)
    save_profile(profile)
    original = motion_key(clip, recipe)
    assert motion_key(clip, recipe) == original
    delete_profile(profile.name)
    removed = motion_key(clip, recipe)
    assert removed != original
    save_profile(profile.model_copy(update={"focal_px": 1200.0}))
    replaced = motion_key(clip, recipe)
    assert replaced != original
    assert replaced != removed
    delete_profile(profile.name)
