"""Library sessions, ratings and inherited adjustment contracts."""

import json
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_clips_api import listed_clip

from mcrawsome.library.scan import Library


def test_ratings_persist_in_annotations_and_refresh_without_reading_clip_facts(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    current = client.get(url).json()
    assert current["values"]["rating"] == 0
    values = current["values"] | {"rating": 5, "tag": "good-take"}
    assert client.put(url, json={"version": current["version"], "values": values}).status_code == 200

    def no_read(*args: object) -> None:
        pytest.fail("A rating refresh must use cached capture facts")

    monkeypatch.setattr(Library, "_read", no_read)
    [clip] = client.get("/api/v1/library").json()["clips"]
    assert clip["rating"] == 5
    assert client.get(url).json()["values"]["tag"] == "good-take"
    assert json.loads(source.with_suffix(".meta").read_text())["details"]["rating"] == 5
    for value in [-1, 6, True, 2.5, "3"]:
        saved = client.get(url).json()
        assert (
            client.put(
                url, json={"version": saved["version"], "values": values | {"rating": value}}
            ).status_code
            == 422
        )


# Every Library filter can be set at once, including shoots, places and recordings made together.
FILTERS = [
    *("camera", "resolution", "fps", "iso", "focal_length_mm", "sensor_bits", "captured", "rating"),
    *("movement", "shoot", "place", "together"),
]


def test_portable_session_round_trip_autorestore_and_missing_sources(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    session = library / "edit.mcrawsome-session.json"
    workspace = {
        "paths": [str(source)],
        "selection": [identity],
        "focused": identity,
        "search": "clip",
        "kind": "raw",
        "sort": "name",
        # Every Library filter can be set at once, including shoots, places and recordings made together.
        "facets": dict.fromkeys(FILTERS, "x") | {"rating": "5"},
        "views": {},
    }
    with closing(TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 12))) as local:
        assert local.put("/api/v1/session", json=workspace).status_code == 200
        saved = local.post("/api/v1/session/save", json={"path": str(session), "portable": True})
        assert saved.status_code == 200, saved.text
        assert saved.json()["recent"] == [str(session)]
        document = json.loads(session.read_text())
        assert document["workspace"]["paths"] == [source.name]
        assert document["snapshots"][0]["path"] == source.name
        assert document["snapshots"][0]["source_id"] == identity
        assert local.post("/api/v1/session/new").status_code == 200
        loaded = local.post("/api/v1/session/load", json={"path": str(session)})
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["workspace"] == workspace
        assert loaded.json()["clips"][0]["id"] == identity
        assert local.get(f"/api/v1/clips/{identity}/recipe").status_code == 200
        from mcrawsome.library.sessions import Sessions

        restored = Sessions().view()
        assert restored.workspace.model_dump(mode="json") == workspace
        assert restored.path == session
        # Moving a portable bundle resolves paths beside its new session file.
        moved = tmp_path / "moved"
        moved.mkdir()
        (moved / source.name).write_bytes(source.read_bytes())
        relocated = moved / session.name
        relocated.write_bytes(session.read_bytes())
        result = local.post("/api/v1/session/load", json={"path": str(relocated)})
        assert result.json()["workspace"]["paths"] == [str(moved / source.name)]
        (moved / source.name).unlink()
        result = local.post("/api/v1/session/load", json={"path": str(relocated)})
        assert result.status_code == 200
        assert result.json()["missing"] == [str(moved / source.name)]
        assert result.json()["workspace"]["selection"] == [identity]


def test_sessions_refuse_remote_clients_and_never_replace_unrelated_files(
    client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "recording.mcraw"
    path.write_bytes(b"recording")
    with closing(TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 12))) as local:
        assert local.post("/api/v1/session/save", json={"path": str(path)}).status_code == 422
        assert path.read_bytes() == b"recording"
        bad = tmp_path / "bad.mcrawsome-session.json"
        bad.write_text('{"format":"other"}')
        assert local.post("/api/v1/session/load", json={"path": str(bad)}).status_code == 422
        assert local.post("/api/v1/session/save", json={"path": str(bad)}).status_code == 422
        assert bad.read_text() == '{"format":"other"}'
    with closing(
        TestClient(client.app, base_url="http://127.0.0.1:8642", client=("192.0.2.1", 12))
    ) as remote:
        for action in ["new", "load", "save"]:
            assert remote.post(f"/api/v1/session/{action}", json={"path": str(path)}).status_code == 403
        assert remote.put("/api/v1/session", json={"paths": []}).status_code == 403


def test_apply_all_updates_inherited_defaults_and_preserves_clip_overrides(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, _ = listed_clip(client, library, make_mcraw)
    recipe_url = f"/api/v1/clips/{identity}/recipe"
    assert (
        client.put(recipe_url, json={"version": "", "values": {"develop": {"exposure": 1}}}).status_code
        == 200
    )
    current = client.get("/api/v1/adjustment-defaults")
    assert current.status_code == 200
    saved = client.put(
        "/api/v1/adjustment-defaults",
        json={
            "version": current.json()["version"],
            "values": {"develop": {"exposure": 2}, "look": "neutral"},
        },
    )
    assert saved.status_code == 200, saved.text
    view = client.get(recipe_url).json()
    assert view["inherited"]["develop"]["exposure"] == 2
    assert view["resolved"]["develop"]["exposure"] == 1
    assert view["layer"]["values"] == {"develop": {"exposure": 1}}
    assert (
        client.put(
            "/api/v1/adjustment-defaults", json={"version": "", "values": {"look": "neutral"}}
        ).status_code
        == 409
    )
    assert (
        client.put(
            "/api/v1/adjustment-defaults",
            json={"version": saved.json()["version"], "values": {"trim": {"start": "1s"}}},
        ).status_code
        == 422
    )


def test_unmount_all_attempts_every_active_mount_and_reports_in_use(
    client: TestClient, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.errors import McError
    from mcrawsome.mount.manager import MountedSequence, MountListing, MountManager

    records = [
        MountedSequence(
            id=name,
            source=library / f"{name}.mcraw",
            path=library / name,
            stamp=(1, 2, 3, 4),
            active=name != "offline",
            frames=3,
        )
        for name in ["busy", "idle", "offline"]
    ]
    calls: list[str] = []

    def unmount(self: MountManager, identity: str) -> MountedSequence:
        calls.append(identity)
        if identity == "busy":
            raise McError(
                "Media is in use. Close its files in the receiving application before unmounting.",
                code="engine-busy",
            )
        return records[1].model_copy(update={"active": False})

    monkeypatch.setattr(
        MountManager, "listing", lambda self: MountListing(available=True, reason="Available", mounts=records)
    )
    monkeypatch.setattr(MountManager, "unmount", unmount)
    response = client.post("/api/v1/mounts/unmount-all")
    assert response.status_code == 200, response.text
    assert calls == ["busy", "idle"]
    results = response.json()["results"]
    assert [(item["id"], item["ok"]) for item in results] == [("busy", False), ("idle", True)]
    assert results[0]["code"] == "engine-busy"
    assert "in use" in results[0]["message"]


def test_named_session_autosaves_changes_and_preserves_an_unreadable_workspace(tmp_path: Path) -> None:
    from mcrawsome.errors import McError
    from mcrawsome.library.sessions import Sessions, Workspace

    sessions = Sessions()
    path = tmp_path / "work.mcrawsome-session.json"
    sessions.save(path, True)
    sessions.update(Workspace(search="arrival", kind="raw", facets={"iso": "100"}))
    assert json.loads(path.read_text())["workspace"]["search"] == "arrival"
    assert Sessions().view().workspace.search == "arrival"
    sessions.record.write_text("broken")
    restored = Sessions()
    assert restored.view().problem
    with pytest.raises(McError, match="Could not restore"):
        restored.update(Workspace())
    assert restored.record.read_text() == "broken"
    assert restored.new().problem is None


def test_open_clips_and_sessions_share_original_paths_without_mounts(
    client: TestClient, tiny_raw: Path, tmp_path: Path
) -> None:
    from contextlib import closing

    from mcrawsome.library.sessions import Sessions

    with closing(
        TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 1234))
    ) as local:
        assert local.post("/api/v1/opened-clips", json={"paths": [str(tiny_raw)]}).status_code == 200
        assert local.get("/api/v1/session").json()["workspace"]["paths"] == [str(tiny_raw)]
        target = tmp_path / "edit.mcrawsome-session.json"
        assert local.post("/api/v1/session/save", json={"path": str(target)}).status_code == 200
        assert local.post("/api/v1/session/new").status_code == 200
        assert local.get("/api/v1/opened-clips").json()["clips"] == []
        loaded = local.post("/api/v1/session/load", json={"path": str(target)})
        assert loaded.status_code == 200
        assert [clip["path"] for clip in local.get("/api/v1/opened-clips").json()["clips"]] == [str(tiny_raw)]
        assert Sessions().view().workspace.paths == [tiny_raw]
        tiny_raw.rename(tmp_path / "offline.mcraw")
        missing = local.get("/api/v1/session").json()
        assert missing["missing"] == [str(tiny_raw)]
        assert missing["workspace"]["paths"] == [str(tiny_raw)]
        assert local.delete("/api/v1/opened-clips").status_code == 200
        assert local.get("/api/v1/session").json()["workspace"]["paths"] == []
