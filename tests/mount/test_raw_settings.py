"""RAW calibration is an immutable part of virtual media, without changing sensor codes."""

import contextlib
import hashlib
import json
from pathlib import Path

import pytest

from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.mount.manager import MountedSequence, MountManager
from mcrawsome.mount.sequence import Sequence, source_stamp
from mcrawsome.output.dng import parse_gain_maps, read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.schema import RawCalibration
from mcrawsome.resolve.bin_archive import mounted_media


@pytest.mark.parametrize(("strength", "mode"), [(0, "full"), (0.5, "full"), (1, "colour")])
@pytest.mark.parametrize("black", [32.5, (30.5, 31.5, 32.5, 33.5)])
def test_virtual_calibration_matches_physical_dng_without_changing_codes(
    tiny_raw: Path, tmp_path: Path, strength: float, mode: str, black: float | tuple[float, ...]
) -> None:
    settings = RawCalibration(
        black_level=black, white_level=3000, lens_shading=strength, lens_shading_mode=mode
    )
    recipe = build_recipe(
        [
            {
                "develop": settings.model_dump(exclude_none=True) | {"exposure": 0, "white_balance": "first"},
                "output": {"kind": "dng", "dng_compression": "none"},
            }
        ]
    )
    exported = export_dng(tiny_raw, tmp_path / "physical", recipe, workers=1)
    with Sequence(tiny_raw, raw_settings=settings) as sequence, Clip(tiny_raw) as clip:
        for i in range(3):
            name = sequence.names()[i]
            path = tmp_path / name
            path.write_bytes(sequence.read(name, 0, sequence.size(name)))
            virtual = read_dng_tags(path)
            physical = read_dng_tags(exported.output / name)
            virtual.pop(273)
            physical.pop(273)
            assert virtual == physical
            expected_black = (black,) * 4 if isinstance(black, float) else black
            assert virtual[50714] == pytest.approx(expected_black)
            assert virtual[50717] == (3000,)
            if strength == 0:
                assert all((plane.gains == 1).all() for plane in parse_gain_maps(bytes(virtual[51009])))
            assert path.read_bytes()[-128 * 64 * 2 :] == clip.raw_frame(i).data.astype("<u2").tobytes()


def test_mount_generations_keep_default_identity_and_remount_their_own_settings(tiny_raw: Path) -> None:
    manager = MountManager()
    if not manager.listing().available:
        pytest.skip(manager.listing().reason)
    mounts = []
    other = MountManager()
    try:
        plain = manager.mount(tiny_raw)
        mounts.append(plain.id)
        old_key = [2, str(tiny_raw.resolve()), source_stamp(tiny_raw), str(plain.mount_root)]
        assert plain.id == hashlib.sha256(json.dumps(old_key).encode()).hexdigest()[:24]
        first = (plain.path / "tiny-000000.dng").read_bytes()
        settings = RawCalibration(black_level=(10, 20, 30, 40), white_level=3000, lens_shading=0)
        adjusted = manager.mount(tiny_raw, raw_settings=settings)
        mounts.append(adjusted.id)
        assert adjusted.id != plain.id
        assert manager.mount(tiny_raw, raw_settings=settings).id == adjusted.id
        assert (plain.path / "tiny-000000.dng").read_bytes() == first
        manager.unmount(adjusted.id)
        remounted = other.mount(tiny_raw, raw_settings=settings)
        assert remounted.path == adjusted.path
        assert remounted.raw_settings == settings
        tags = read_dng_tags(remounted.path / "tiny-000000.dng")
        assert tags[50714] == (10, 20, 30, 40)
        assert tags[50717] == (3000,)
        assert all((plane.gains == 1).all() for plane in parse_gain_maps(bytes(tags[51009])))
        descriptor = mounted_media(remounted)
        assert descriptor.info.black_levels == (10, 20, 30, 40)
        assert descriptor.info.white_level == 3000
        assert all((plane.gains == 1).all() for plane in parse_gain_maps(descriptor.info.gain_maps))
    finally:
        for identity in mounts:
            with contextlib.suppress(McError):  # the first unmount removes the record
                other.unmount(identity)
        for owner in (manager, other):  # each waits for the services it started
            for process in owner.processes.values():
                process.wait(5)


def test_old_mount_records_keep_recorded_calibration(tiny_raw: Path) -> None:
    record = MountedSequence.model_validate(
        {
            "id": "0" * 24,
            "source": tiny_raw,
            "path": tiny_raw.parent / "media",
            "stamp": source_stamp(tiny_raw),
        }
    )
    assert record.raw_settings == RawCalibration()


def test_calibrated_headers_do_not_decode_sensor_data(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object) -> None:
        raise AssertionError("decoded for calibration metadata")

    monkeypatch.setattr(Clip, "raw_frame", forbidden)
    with Sequence(tiny_raw, raw_settings=RawCalibration(black_level=42, white_level=3000)) as sequence:
        assert sequence.frame_info(0).black_levels == (42,) * 4
        assert sequence.read(sequence.names()[0], 0, 8) == b"II*\0\x08\0\0\0"
