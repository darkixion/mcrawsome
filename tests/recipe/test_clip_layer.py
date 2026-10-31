import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from mcrawsome.errors import McError
from mcrawsome.output.plan import resolve_recipe
from mcrawsome.output.sidecar import quick_hash, sidecar_path
from mcrawsome.paths import state_dir
from mcrawsome.recipe.clip_layer import minimal_values, path_for, read_layer, write_layer
from mcrawsome.recipe.load import RecipeError, build_recipe


@pytest.fixture
def clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "clips" / "my.clip.mcraw"
    path.parent.mkdir()
    path.write_bytes(b"synthetic input")
    return path


def test_absent_layer_has_an_empty_version(clip: Path) -> None:
    layer = read_layer(clip)
    assert layer.values == {}
    assert layer.version == ""
    assert layer.error is None
    assert path_for(clip) == clip.with_name("my.clip.mcrawsome.toml")
    assert path_for(clip) != sidecar_path(clip)


def test_read_only_folder_uses_state_and_keeps_using_it_when_writable(clip: Path) -> None:
    key = hashlib.sha256(f"{clip.resolve()}|{clip.stat().st_size}|{quick_hash(clip)}".encode()).hexdigest()[
        :20
    ]
    expected_path = state_dir() / "clip-recipes" / f"{key}.toml"
    clip.parent.chmod(0o555)
    try:
        assert path_for(clip) == expected_path
        saved = write_layer(clip, {"develop": {"exposure": 2}}, "")
        assert read_layer(clip) == saved
        assert expected_path.exists()
        assert not clip.with_name("my.clip.mcrawsome.toml").exists()
    finally:
        clip.parent.chmod(0o755)
    assert path_for(clip) == expected_path
    assert read_layer(clip) == saved


def test_existing_beside_layer_is_used_in_a_read_only_folder(clip: Path) -> None:
    beside = path_for(clip)
    beside.write_text("hdr = true\n")
    clip.parent.chmod(0o555)
    try:
        assert path_for(clip) == beside
        assert read_layer(clip).values == {"hdr": True}
    finally:
        clip.parent.chmod(0o755)


def test_stale_write_and_delete_leave_the_file_unchanged(clip: Path) -> None:
    saved = write_layer(clip, {"output": {"crf": 12}}, "")
    content = path_for(clip).read_bytes()
    assert saved.version == hashlib.sha256(content).hexdigest()
    for values in ({"output": {"crf": 10}}, {}):
        with pytest.raises(McError) as err:
            write_layer(clip, values, "")
        assert err.value.code == "recipe-changed"
        assert path_for(clip).read_bytes() == content
    deleted = write_layer(clip, {}, saved.version)
    assert deleted.values == {}
    assert deleted.version == ""
    assert deleted.error is None
    assert not path_for(clip).exists()


def test_only_one_concurrent_write_can_use_the_same_version(clip: Path) -> None:
    saved = write_layer(clip, {"output": {"crf": 12}}, "")
    ready = Barrier(2)

    def save(crf: int) -> str:
        ready.wait(timeout=10)
        try:
            write_layer(clip, {"output": {"crf": crf}}, saved.version)
        except McError as exc:
            return exc.code
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (10, 14)))
    assert sorted(results) == ["recipe-changed", "saved"]
    layer = read_layer(clip)
    assert layer.error is None
    assert layer.values["output"]["crf"] in (10, 14)


def test_failed_atomic_replace_keeps_the_original_and_removes_the_temporary_file(
    clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = write_layer(clip, {"hdr": True}, "")
    original = path_for(clip).read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        assert self.read_bytes() != original
        assert target.read_bytes() == original
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_layer(clip, {"hdr": False}, saved.version)
    assert path_for(clip).read_bytes() == original
    assert not list(clip.parent.glob("*.part"))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[broken", "TOML"),
        ("[output]\ndng_scale = 'invalid'\n", "output.dng_scale"),
        ("[trim]\nstart = 'bad'\n", "trim.start"),
    ],
)
def test_broken_layer_can_only_be_replaced_with_its_version(clip: Path, content: str, message: str) -> None:
    path_for(clip).write_text(content)
    broken = read_layer(clip)
    assert broken.error is not None
    assert message in broken.error
    assert broken.version == hashlib.sha256(content.encode()).hexdigest()
    with pytest.raises(McError) as err:
        write_layer(clip, {"hdr": True}, "")
    assert err.value.code == "recipe-changed"
    assert path_for(clip).read_text() == content
    repaired = write_layer(clip, {"hdr": True}, broken.version)
    assert repaired.error is None
    assert repaired.values == {"hdr": True}


def test_write_validates_the_full_recipe_before_changing_the_file(clip: Path) -> None:
    (clip.parent / "mcrawsome.toml").write_text("hdr = true\n")
    saved = write_layer(clip, {"output": {"crf": 12}}, "")
    with pytest.raises(RecipeError, match=r"output\.kind"):
        write_layer(clip, {"output": {"kind": "invalid"}}, saved.version)
    assert read_layer(clip) == saved
    assert not list(clip.parent.glob("*.part"))


def test_minimal_values_compares_against_lower_layers_and_builtin_defaults(clip: Path) -> None:
    (clip.parent / "mcrawsome.toml").write_text("[output]\ndng_scale = 'half'\ndng_compression = 'none'\n")
    write_layer(clip, {"output": {"crf": 12}}, "")
    values: dict[str, Any] = build_recipe(
        [{"output": {"dng_scale": "half", "dng_compression": "lossless"}}, {"trim": {"start": "2"}}]
    ).model_dump(mode="json", exclude_none=True)
    assert minimal_values(clip, values) == {"output": {"dng_compression": "lossless"}, "trim": {"start": "2"}}


@pytest.mark.parametrize("value", ["2", "00:00:02.5", "48f", "1:05.5"])
def test_trim_uses_the_shared_time_syntax(value: str) -> None:
    recipe = build_recipe([{"trim": {"start": value, "duration": "1"}}])
    assert recipe.trim.start == value
    assert recipe.trim.duration == "1"


@pytest.mark.parametrize("value", ["bad", "-1", "00:60:00", "00:00:75", ""])
def test_invalid_trim_is_a_recipe_problem(value: str) -> None:
    with pytest.raises(RecipeError, match=r"trim\.duration"):
        build_recipe([{"trim": {"duration": value}}])


def test_minimal_values_drop_cleared_values_so_they_can_be_saved(clip: Path) -> None:
    (clip.parent / "mcrawsome.toml").write_text("[camera]\nfocal_px = 1500\n")
    values: dict[str, Any] = build_recipe([{"develop": {"exposure": 1}}]).model_dump(mode="json")
    assert values["camera"]["focal_px"] is None
    minimal = minimal_values(clip, values)
    assert minimal == {"develop": {"exposure": 1}}
    assert write_layer(clip, minimal, "").values == minimal


@pytest.mark.parametrize(
    ("values", "field"),
    [({"grade": {"skin_sat": None}}, "grade.skin_sat"), ({"output": {"crf": 12}, "look": None}, "look")],
)
def test_write_refuses_a_value_that_clears_a_setting(clip: Path, values: dict[str, Any], field: str) -> None:
    with pytest.raises(RecipeError) as err:
        write_layer(clip, values, "")
    assert err.value.field == field
    assert field in str(err.value)
    assert not path_for(clip).exists()


@pytest.mark.parametrize(("value", "text"), [(2, "2"), (1.5, "1.5")])
def test_trim_accepts_numbers_as_seconds(clip: Path, value: float, text: str) -> None:
    assert build_recipe([{"trim": {"start": value, "duration": value}}]).trim.start == text
    path_for(clip).write_text(f"[trim]\nstart = {value}\n")
    layer = read_layer(clip)
    assert layer.error is None
    assert resolve_recipe(clip, [], None, {}, ()).trim.start == text


def test_trim_refuses_a_boolean() -> None:
    with pytest.raises(RecipeError, match=r"trim\.start"):
        build_recipe([{"trim": {"start": True}}])


def test_a_writable_folder_without_a_layer_reads_no_clip_bytes(
    clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.recipe import clip_layer

    def no_hash(path: Path) -> str:
        raise AssertionError(f"read {path}")

    monkeypatch.setattr(clip_layer, "quick_hash", no_hash)
    assert path_for(clip) == clip.with_name("my.clip.mcrawsome.toml")
    assert read_layer(clip).values == {}
    resolve_recipe(clip, [], None, {}, ())


def test_save_keeps_an_existing_file_mode(clip: Path) -> None:
    saved = write_layer(clip, {"hdr": True}, "")
    path_for(clip).chmod(0o640)
    write_layer(clip, {"hdr": False}, saved.version)
    assert path_for(clip).stat().st_mode & 0o7777 == 0o640


def test_a_new_layer_takes_the_umask_default_and_is_synced(
    clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    old = os.umask(0o027)
    try:
        write_layer(clip, {"hdr": True}, "")
    finally:
        os.umask(old)
    assert path_for(clip).stat().st_mode & 0o7777 == 0o640
    assert synced


def test_cleared_value_carries_its_field(clip: Path) -> None:
    with pytest.raises(RecipeError) as error:
        write_layer(clip, {"grade": {"skin_sat": None}}, "")
    assert error.value.field == "grade.skin_sat"
