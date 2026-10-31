from pathlib import Path

import pytest

from mcrawsome.mount.manager import MountedSequence, MountManager
from mcrawsome.recipe.raw import RawCalibration
from mcrawsome.resolve.media_mounts import prepare_media_mounts


def _mount(tmp_path: Path, size: str = "full", rotation: int = 0) -> MountedSequence:
    return MountedSequence(
        id="a" * 24,
        source=tmp_path / "capture.mcraw",
        path=tmp_path / "selected",
        mount_root=tmp_path / "mounts",
        stamp=(1, 2, 3, 4),
        frames=20,
        frame_range=(7, 20),
        quad_bayer_remosaic=False,
        raw_settings=RawCalibration.model_validate(
            {
                "mount_proxy": size,
                "rotation": rotation,
                "black_level": 42,
                "mount_frame_rate": "integer",
                "mount_shading": "baked",
            }
        ),
    )


@pytest.mark.parametrize("linked", [False, True])
def test_full_size_recorded_media_needs_no_auxiliary_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: bool
) -> None:
    selected = _mount(tmp_path)
    monkeypatch.setattr(MountManager, "mount", lambda *_a, **_k: pytest.fail("mounted redundant media"))
    prepared = prepare_media_mounts(MountManager(), [selected], linked_proxies=linked)
    assert prepared.originals == [selected]
    assert prepared.proxies == [None]
    assert prepared.dependencies == [selected]
    assert prepared.rotations is None


@pytest.mark.parametrize("size", ["half", "quarter", "eighth"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pair_preserves_snapshot_frame_range_and_recorded_pixel_orientation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: str, rotation: int
) -> None:
    selected = _mount(tmp_path, size, rotation)
    calls: list[RawCalibration] = []

    def mount(_manager: MountManager, source: Path, **kwargs: object) -> MountedSequence:
        assert source == selected.source
        assert kwargs["expected"] == selected.stamp
        assert kwargs["frame_range"] == selected.frame_range
        assert kwargs["quad_bayer_remosaic"] is False
        assert kwargs["location"] == selected.mount_root
        raw = kwargs["raw_settings"]
        assert isinstance(raw, RawCalibration)
        assert raw.rotation == 0
        assert raw.model_copy(update={"mount_proxy": size, "rotation": rotation}) == selected.raw_settings
        calls.append(raw)
        return selected.model_copy(
            update={
                "id": str(len(calls)) * 24,
                "path": tmp_path / raw.mount_proxy,
                "raw_settings": raw,
            }
        )

    monkeypatch.setattr(MountManager, "mount", mount)
    prepared = prepare_media_mounts(MountManager(), [selected], linked_proxies=True)
    original, proxy = prepared.originals[0], prepared.proxies[0]
    assert proxy is not None
    assert original.raw_settings.mount_proxy == "full"
    assert proxy.raw_settings.mount_proxy == size
    assert original.path != proxy.path
    assert prepared.dependencies == [original, proxy]
    assert prepared.rotations == ([rotation] if rotation else None)
    assert len(calls) == (2 if rotation else 1)
    assert selected.raw_settings.rotation == rotation
    assert selected.raw_settings.mount_proxy == size


def test_unverified_proxy_writer_keeps_existing_media_and_guard_behavior(tmp_path: Path) -> None:
    selected = _mount(tmp_path, "quarter")
    prepared = prepare_media_mounts(MountManager(), [selected])
    assert prepared.originals == [selected]
    assert prepared.proxies == [None]


@pytest.mark.parametrize(
    "changed", ["source", "stamp", "frames", "frame_range", "quad_bayer_remosaic", "path", "id"]
)
def test_mismatched_original_and_proxy_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    selected = _mount(tmp_path, "quarter")
    values: dict[str, object] = {
        "source": tmp_path / "other.mcraw",
        "stamp": (2, 3, 4, 5),
        "frames": 19,
        "frame_range": (0, 20),
        "quad_bayer_remosaic": True,
        "path": selected.path,
        "id": selected.id,
    }
    original = selected.model_copy(
        update={
            "id": "b" * 24,
            "path": tmp_path / "original",
            "raw_settings": selected.raw_settings.model_copy(update={"mount_proxy": "full"}),
        }
    ).model_copy(update={changed: values[changed]})
    monkeypatch.setattr(MountManager, "mount", lambda *_a, **_k: original)
    with pytest.raises(ValueError, match="immutable capture"):
        prepare_media_mounts(MountManager(), [selected], linked_proxies=True)
