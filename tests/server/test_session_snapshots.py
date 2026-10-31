"""Sessions recover complete clip decisions without mounts or inherited defaults."""

import json
from pathlib import Path

import pytest

from mcrawsome.errors import McError
from mcrawsome.library.annotations import ClipAnnotations, read_annotations, write_annotations
from mcrawsome.library.scan import clip_id, clip_recipe
from mcrawsome.library.sessions import ClipView, Sessions, Workspace
from mcrawsome.recipe.clip_layer import read_layer, write_layer
from mcrawsome.recipe.schema import Recipe


def resolved(source: Path) -> Recipe:
    return clip_recipe(source, read_layer(source))


def test_full_snapshot_roundtrip_defaults_and_independent_sessions(tiny_raw: Path, tmp_path: Path) -> None:
    source = tiny_raw
    identity = clip_id(source)
    original = source.read_bytes()
    folder = source.parent / "mcrawsome.toml"
    folder.write_text("[develop]\nexposure = 1.25\n")
    write_layer(
        source,
        {
            "trim": {"start": "2f", "duration": "3f"},
            "develop": {"rotation": 90, "mount_proxy": "half"},
            "motion": {"markers": True, "horizon_lock": True},
        },
        "",
    )
    details = ClipAnnotations(
        title="Scene",
        description="Description",
        keywords=["one", "two"],
        scene="1",
        shot="A",
        take="2",
        camera="B",
        location="Garden",
        tag="good-take",
        rating=5,
        notes="First line\nSecond line",
    )
    write_annotations(source, identity, details, "")
    expected = resolved(source)
    sessions = Sessions()
    workspace = Workspace(paths=[source], views={identity: ClipView(overlay="focus", resolution="source")})
    sessions.update(workspace)
    first = tmp_path / "first.mcrawsome-session.json"
    sessions.save(first, False)
    saved = json.loads(first.read_text())["snapshots"][0]
    assert saved["recipe"] == expected.model_dump(mode="json")
    assert saved["annotations"] == details.model_dump(mode="json")
    sessions.new()
    # Change inheritance, optional values, notes and preview preferences between sessions.
    folder.write_text('[develop]\nexposure = -3.0\nrotation = 180\n[trim]\nstart = "4f"\n')
    layer = read_layer(source)
    write_layer(source, {}, layer.version)
    record = read_annotations(source, identity)
    write_annotations(source, identity, ClipAnnotations(notes="Other session"), record.version)
    sessions.update(Workspace(paths=[source], views={identity: ClipView(overlay="iso")}))
    second = tmp_path / "second.mcrawsome-session.json"
    sessions.save(second, False)
    other = resolved(source)
    restored = sessions.load(first)
    assert restored.workspace.views[identity] == ClipView(overlay="focus", resolution="source")
    assert resolved(source) == expected
    assert read_annotations(source, identity).values == details
    sessions.load(second)
    assert resolved(source) == other
    assert read_annotations(source, identity).values == ClipAnnotations(notes="Other session")
    assert source.read_bytes() == original


def test_session_pins_unset_values_and_missing_media_until_reconnection(
    tiny_raw: Path, tmp_path: Path
) -> None:
    source = tiny_raw
    identity = clip_id(source)
    sessions = Sessions()
    sessions.update(Workspace(paths=[source]))
    target = tmp_path / "saved.mcrawsome-session.json"
    sessions.save(target, True)
    moved = source.with_suffix(".absent")
    source.rename(moved)
    assert sessions.load(target).missing == [source]
    sessions.update(sessions.state.document.workspace)
    assert len(json.loads(target.read_text())["snapshots"]) == 1
    source.parent.joinpath("mcrawsome.toml").write_text('[trim]\nstart = "4f"\n[develop]\nrotation = 180\n')
    moved.rename(source)
    sessions.view()
    assert resolved(source) == Recipe()
    assert read_annotations(source, identity).values == ClipAnnotations()
    # Empty explicit overrides must continue masking folder defaults after a reset/save.
    layer = read_layer(source)
    write_layer(source, {}, layer.version)
    assert resolved(source) == Recipe()


def test_changed_source_refuses_restore_before_any_settings_are_written(
    tiny_raw: Path, tmp_path: Path
) -> None:
    source = tiny_raw
    sessions = Sessions()
    sessions.update(Workspace(paths=[source]))
    path = tmp_path / "saved.mcrawsome-session.json"
    sessions.save(path, False)
    data = json.loads(path.read_text())
    second = source.with_name("second.mcraw")
    second.write_bytes(source.read_bytes())
    data["workspace"]["paths"].append(str(second))
    data["snapshots"][0]["recipe"]["develop"]["rotation"] = 90
    data["snapshots"].append({**data["snapshots"][0], "path": str(second), "source_id": "0" * 16})
    path.write_text(json.dumps(data))
    before = read_layer(source)
    with pytest.raises(McError, match="recording changed"):
        sessions.load(path)
    assert read_layer(source) == before
    assert sessions.state.document.snapshots[0].recipe.develop.rotation == 0


def test_schema_one_session_remains_loadable(tiny_raw: Path, tmp_path: Path) -> None:
    path = tmp_path / "old.mcrawsome-session.json"
    path.write_text(
        json.dumps(
            {"format": "mcrawsome.session", "schema_version": 1, "workspace": {"paths": [str(tiny_raw)]}}
        )
    )
    assert Sessions().load(path).workspace.paths == [tiny_raw]


def test_restart_restores_saved_recipe_and_notes(tiny_raw: Path, tmp_path: Path) -> None:
    source = tiny_raw
    identity = clip_id(source)
    sessions = Sessions()
    sessions.update(Workspace(paths=[source]))
    write_layer(source, {"develop": {"rotation": 90}}, "")
    write_annotations(source, identity, ClipAnnotations(notes="Restore this"), "")
    sessions.clip_changed(source)
    assert sessions.state.document.snapshots[0].recipe.develop.rotation == 90
    # Simulate external changes after the app last closed.
    write_layer(source, {}, read_layer(source).version)
    write_annotations(
        source, identity, ClipAnnotations(notes="Changed"), read_annotations(source, identity).version
    )
    restored = Sessions().view()
    assert restored.problem is None
    assert resolved(source).develop.rotation == 90
    assert read_annotations(source, identity).values.notes == "Restore this"


def test_failed_restore_rolls_back_settings_and_keeps_current_session(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io.atomic import write_atomic
    from mcrawsome.library import sessions as module

    source = tiny_raw
    sessions = Sessions()
    sessions.update(Workspace(paths=[source]))
    target = tmp_path / "saved.mcrawsome-session.json"
    sessions.save(target, False)
    data = json.loads(target.read_text())
    data["snapshots"][0]["recipe"]["develop"]["rotation"] = 90
    target.write_text(json.dumps(data))
    original = write_atomic

    def fail(path: Path, raw: bytes) -> None:
        if path == sessions.record:
            raise OSError("Test disk failure")
        original(path, raw)

    monkeypatch.setattr(module, "write_atomic", fail)
    with pytest.raises(McError, match="auto-save"):
        sessions.load(target)
    assert resolved(source).develop.rotation == 0
    assert sessions.state.document.snapshots[0].recipe.develop.rotation == 0
