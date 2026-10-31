"""Saved display turns affect DNG tags and identities without changing sensor samples."""

import hashlib
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from mcrawsome.io.clip import Clip
from mcrawsome.io.orientation import dng_orientation
from mcrawsome.mount.manager import MountManager, availability
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import read_dng_tags
from mcrawsome.recipe.raw import RawCalibration


@pytest.mark.parametrize("recorded", [0, 1, 2, 3])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_saved_turn_composes_with_motioncam_orientation(recorded: int, rotation: int) -> None:
    base = 2 if recorded == 3 else 0
    assert dng_orientation(recorded, rotation) == (1, 6, 3, 8)[(base + rotation // 90) % 4]


@pytest.mark.parametrize("value", [-90, 45, 360, "sideways"])
def test_invalid_saved_turn_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        RawCalibration(rotation=value)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_virtual_rotation_changes_only_orientation_tag(tiny_raw: Path, tmp_path: Path, rotation: int) -> None:
    original = hashlib.sha256(tiny_raw.read_bytes()).digest()
    with (
        Sequence(tiny_raw, raw_settings=RawCalibration(rotation=rotation)) as sequence,
        Clip(tiny_raw) as clip,
    ):
        raw = clip.raw_frame(0)
        name = sequence.names()[0]
        size = sequence.size(name)
        data = b"".join(sequence.read(name, at, 997) for at in range(0, size, 997))
        path = tmp_path / "frame.dng"
        path.write_bytes(data)
        tags = read_dng_tags(path)
        assert tags[274] == (dng_orientation(raw.metadata.orientation, rotation),)
        assert tags[256] == (raw.metadata.width,)
        assert tags[257] == (raw.metadata.height,)
        np.testing.assert_array_equal(np.frombuffer(data[-raw.data.nbytes :], "<u2"), raw.data.ravel())
    assert hashlib.sha256(tiny_raw.read_bytes()).digest() == original


def test_orientation_creates_a_new_mount_identity(tiny_raw: Path) -> None:
    if not availability()[0]:
        pytest.skip("FUSE unavailable")
    manager = MountManager()
    first = manager.mount(tiny_raw)
    try:
        second = manager.mount(tiny_raw, raw_settings=RawCalibration(rotation=180))
        try:
            assert first.id != second.id
            assert first.path != second.path
            assert manager.get(first.id).raw_settings.rotation == 0
            assert manager.get(second.id).raw_settings.rotation == 180
        finally:
            manager.unmount(second.id)
    finally:
        manager.unmount(first.id)
