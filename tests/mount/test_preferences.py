from pathlib import Path

import pytest

from mcrawsome.mount.manager import MountManager
from mcrawsome.server.settings import InterfaceSettings, save_settings


def test_mount_folder_is_user_configurable_and_existing_paths_stay_stable(
    tiny_raw: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCRAWSOME_MOUNT_DIR", raising=False)
    selected = tmp_path / "My Mounts"
    save_settings(InterfaceSettings(mount_folder=selected))
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounted = manager.mount(tiny_raw)
    try:
        assert mounted.path.is_relative_to(selected)
        save_settings(InterfaceSettings(mount_folder=tmp_path / "elsewhere"))
        assert manager.get(mounted.id).path == mounted.path
    finally:
        manager.unmount(mounted.id)
    again = manager.mount(tiny_raw, location=selected)  # the record went with the unmount
    try:
        assert again.path == mounted.path
    finally:
        manager.unmount(again.id)


def test_time_of_day_timecode_is_a_new_mount_generation_in_the_chosen_zone(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io.timecode import local_zone

    monkeypatch.setenv("MCRAWSOME_MOUNT_DIR", str(tmp_path / "mounts"))
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    plain = manager.mount(tiny_raw)
    try:
        assert plain.timecode is None
        save_settings(InterfaceSettings(timecode="time-of-day"))
        timed = manager.mount(tiny_raw)
        try:
            assert timed.timecode == local_zone()
            assert timed.id != plain.id  # served bytes differ: never the same identity
            assert manager.get(timed.id).timecode == timed.timecode
            assert manager.mount(tiny_raw, timecode=None).id == plain.id
        finally:
            manager.unmount(timed.id)
    finally:
        manager.unmount(plain.id)


def test_the_location_zone_comes_from_the_gps_fix_or_falls_back_to_this_computer(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io.timecode import local_zone
    from mcrawsome.library import places
    from mcrawsome.mount.manager import timecode_zone

    assert timecode_zone("zero", "location", tiny_raw) is None
    assert timecode_zone("time-of-day", "computer", tiny_raw) == local_zone()
    monkeypatch.setattr(places, "recording_zone", lambda gps: "Pacific/Auckland")
    assert timecode_zone("time-of-day", "location", tiny_raw) == "Pacific/Auckland"
    monkeypatch.setattr(places, "recording_zone", lambda gps: None)
    assert timecode_zone("time-of-day", "location", tiny_raw) == local_zone()
