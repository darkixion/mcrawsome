"""Editable production details are separate from recorded metadata and development settings."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from test_clips_api import listed_clip

from mcrawsome.errors import McError


def test_annotations_save_beside_clip_and_reload_without_changing_source(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    before = source.read_bytes()
    url = f"/api/v1/clips/{identity}/annotations"
    empty = client.get(url)
    assert empty.status_code == 200, empty.text
    assert empty.json()["version"] == ""
    values = {
        "title": "Bentley House exterior",
        "description": "Arrival, looking towards the gate.",
        "keywords": ["exterior", "arrival"],
        "scene": "12B",
        "shot": "C",
        "take": "14A",
        "location": "Bentley House Exterior",
        "tag": "good-take",
        "notes": "Keep the end.\nClean sound.",
        "camera": "A",
        "rating": 0,
    }
    saved = client.put(url, json={"version": "", "values": values})
    assert saved.status_code == 200, saved.text
    record = saved.json()
    assert record["values"] == values
    assert record["storage"] == "sidecar"
    assert client.get(url).json() == record
    document = json.loads(source.with_suffix(".meta").read_text())
    assert document["format"] == "mcrawsome.clip-details"
    assert document["schema_version"] == 1
    assert document["source_id"] == identity
    assert document["details"] == {key: value for key, value in values.items() if key != "rating"}
    assert source.read_bytes() == before
    assert client.get(f"/api/v1/clips/{identity}/recipe").json()["layer"]["values"] == {}
    assert client.put(url, json={"version": "", "values": values}).status_code == 409
    invalid = client.put(url, json={"version": record["version"], "values": {"title": "x" * 513}})
    assert invalid.status_code == 422
    assert client.get(url).json() == record


def test_the_library_lists_the_words_a_search_matches(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, _ = listed_clip(client, library, make_mcraw)
    values = {"title": "Harbour wall", "keywords": ["sunrise", "boats"], "scene": "12B", "notes": "Keep it."}
    saved = client.put(f"/api/v1/clips/{identity}/annotations", json={"version": "", "values": values})
    assert saved.is_success, saved.text
    listed = next(clip for clip in client.get("/api/v1/library").json()["clips"] if clip["id"] == identity)
    assert listed["details_text"].split("\n") == ["Harbour wall", "sunrise", "boats", "Keep it.", "12B"]


def test_unknown_sidecar_fields_survive_edits_and_identity_is_checked(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    sidecar = source.with_suffix(".meta")
    sidecar.write_text(
        json.dumps(
            {
                "format": "mcrawsome.clip-details",
                "schema_version": 1,
                "source_id": identity,
                "extra": {"future": True},
                "details": {"title": "Original", "future_field": "keep"},
            }
        )
    )
    current = client.get(url).json()
    updated = client.put(
        url, json={"version": current["version"], "values": current["values"] | {"title": "Renamed"}}
    )
    assert updated.status_code == 200, updated.text
    document = json.loads(sidecar.read_text())
    assert document["extra"] == {"future": True}
    assert document["details"]["future_field"] == "keep"
    assert updated.json()["document"] == document
    document["source_id"] = "another-recording"
    sidecar.write_text(json.dumps(document))
    record = client.get(url).json()
    assert record["storage"] == "local"
    assert record["values"]["title"] == ""
    assert client.put(url, json={"version": "", "values": {"title": "New source"}}).status_code == 200
    assert json.loads(sidecar.read_text()) == document


@pytest.mark.parametrize(
    "existing", [b'{"other_app":true}', b"not JSON", b"x" * 262145], ids=["unrelated", "invalid", "oversized"]
)
def test_unrelated_or_unreadable_json_is_never_overwritten(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], existing: bytes
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    sidecar = source.with_suffix(".meta")
    sidecar.write_bytes(existing)
    url = f"/api/v1/clips/{identity}/annotations"
    record = client.get(url).json()
    assert record["storage"] == "local"
    saved = client.put(url, json={"version": record["version"], "values": {"location": "Garden"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["values"]["location"] == "Garden"
    assert sidecar.read_bytes() == existing


def test_read_only_folder_keeps_details_locally_and_exports_portable_document(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    library.chmod(0o555)
    try:
        saved = client.put(url, json={"version": "", "values": {"take": "14"}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["storage"] == "local"
        assert saved.json()["document"]["details"]["take"] == "14"
        assert not source.with_suffix(".meta").exists()
    finally:
        library.chmod(0o755)
    assert client.get(url).json() == saved.json()


def test_symlink_json_is_left_untouched(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    outside = library.parent / "outside.json"
    outside.write_text('{"private": true}')
    source.with_suffix(".meta").symlink_to(outside)
    url = f"/api/v1/clips/{identity}/annotations"
    assert client.put(url, json={"version": "", "values": {"title": "Safe"}}).status_code == 200
    assert outside.read_text() == '{"private": true}'
    assert source.with_suffix(".meta").is_symlink()


def test_annotation_versions_serialise_concurrent_writers(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    from mcrawsome.library.annotations import ClipAnnotations, write_annotations

    identity, source = listed_clip(client, library, make_mcraw)
    ready = Barrier(2)

    def save(title: str) -> str:
        ready.wait(timeout=5)
        try:
            write_annotations(source, identity, ClipAnnotations(title=title), "")
        except McError as exc:
            return exc.code
        return "saved"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(save, ["One", "Two"]))
    assert sorted(results) == ["recipe-changed", "saved"]


@pytest.mark.parametrize(
    "values",
    [
        {"tag": "yes"},
        {"tag": ["good-take", "rejected"]},
        {"keywords": ["a"] * 65},
        {"take": "x\x00"},
        {"unknown": True},
    ],
)
def test_invalid_annotation_values_are_refused(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], values: dict[str, object]
) -> None:
    identity, _ = listed_clip(client, library, make_mcraw)
    assert (
        client.put(
            f"/api/v1/clips/{identity}/annotations", json={"version": "", "values": values}
        ).status_code
        == 422
    )


def test_annotations_reject_missing_sources(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    source.unlink()
    for missing in [identity, "unknown"]:
        url = f"/api/v1/clips/{missing}/annotations"
        assert client.get(url).status_code == 404
        assert client.put(url, json={"version": "", "values": {"title": "Missing"}}).status_code == 404


def test_existing_owned_details_are_copied_to_local_storage_when_folder_becomes_read_only(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    first = client.put(url, json={"version": "", "values": {"title": "Original"}}).json()
    before = source.with_suffix(".meta").read_bytes()
    library.chmod(0o555)
    try:
        changed = client.put(
            url, json={"version": first["version"], "values": first["values"] | {"take": "14"}}
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["values"]["title"] == "Original"
        assert changed.json()["storage"] == "local"
        assert source.with_suffix(".meta").read_bytes() == before
    finally:
        library.chmod(0o755)
    assert client.get(url).json() == changed.json()


def test_changed_recording_and_future_sidecar_format_are_never_overwritten(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    future = {
        "format": "mcrawsome.clip-details",
        "schema_version": 2,
        "source_id": identity,
        "details": {"title": "Future"},
    }
    sidecar = source.with_suffix(".meta")
    sidecar.write_text(json.dumps(future))
    assert client.get(url).status_code == 422
    assert client.put(url, json={"version": "", "values": {}}).status_code == 422
    assert json.loads(sidecar.read_text()) == future
    source.write_bytes(b"a different source")
    assert client.put(url, json={"version": "", "values": {}}).status_code == 404
    assert json.loads(sidecar.read_text()) == future


def test_broken_local_storage_is_reported_without_replacing_it(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    from mcrawsome.paths import state_dir

    identity, _ = listed_clip(client, library, make_mcraw)
    folder = state_dir() / "clip-details"
    folder.mkdir(parents=True)
    local = folder / f"{identity}.meta"
    local.write_text("broken local details")
    url = f"/api/v1/clips/{identity}/annotations"
    assert client.get(url).status_code == 422
    assert client.put(url, json={"version": "", "values": {"title": "Replace"}}).status_code == 422
    assert local.read_text() == "broken local details"


def test_portable_sidecar_follows_a_renamed_recording(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    values = {"title": "Café <Gate> 🎬", "take": "14A", "tag": "good-take"}
    saved = client.put(url, json={"version": "", "values": values}).json()
    renamed = library / "renamed.mcraw"
    source.rename(renamed)
    source.with_suffix(".meta").rename(renamed.with_suffix(".meta"))
    client.get("/api/v1/library")
    restored = client.get(url).json()
    for key in ("values", "version", "document"):
        assert restored[key] == saved[key]
    assert "renamed.meta" in restored["message"]
    assert json.loads(renamed.with_suffix(".meta").read_text())["details"]["title"] == values["title"]


def test_failed_annotation_write_leaves_the_saved_record_unchanged(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    saved = client.put(url, json={"version": "", "values": {"title": "Before"}}).json()
    original = source.with_suffix(".meta").read_bytes()

    def fail(path: Path, content: bytes) -> None:
        raise OSError("disk is read-only")

    monkeypatch.setattr("mcrawsome.library.annotations.write_atomic", fail)
    result = client.put(url, json={"version": saved["version"], "values": {"title": "After"}})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "output-unwritable"
    assert source.with_suffix(".meta").read_bytes() == original


def test_legacy_audio_preference_is_readable_but_no_longer_editable(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    sidecar = source.with_suffix(".meta")
    sidecar.write_text(
        json.dumps(
            {
                "format": "mcrawsome.clip-details",
                "schema_version": 1,
                "source_id": identity,
                "details": {"title": "Old clip", "link_audio": False},
            }
        )
    )
    url = f"/api/v1/clips/{identity}/annotations"
    before = client.get(url).json()
    assert before["values"]["title"] == "Old clip"
    assert "link_audio" not in before["values"]
    saved = client.put(url, json={"version": before["version"], "values": {"title": "Updated"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["values"]["title"] == "Updated"
    assert "link_audio" not in saved.json()["values"]
    assert json.loads(sidecar.read_text())["details"]["link_audio"] is False
    assert (
        client.put(
            url, json={"version": saved.json()["version"], "values": {"link_audio": False}}
        ).status_code
        == 422
    )
    schema = client.get("/api/v1/openapi.json").json()
    assert "link_audio" not in schema["components"]["schemas"]["ClipAnnotations"]["properties"]


def test_default_details_create_no_sidecar_and_unrelated_legacy_json_survives(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    legacy = source.with_suffix(".json")
    legacy.write_text('{"other_app": true}')
    url = f"/api/v1/clips/{identity}/annotations"
    assert client.put(url, json={"version": "", "values": {}}).status_code == 200
    assert not source.with_suffix(".meta").exists()
    assert client.put(url, json={"version": "", "values": {"notes": "Keep"}}).status_code == 200
    assert json.loads(source.with_suffix(".meta").read_text())["details"] == {"notes": "Keep"}
    assert legacy.read_text() == '{"other_app": true}'


def test_legacy_json_is_not_migrated_by_the_application(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    legacy = source.with_suffix(".json")
    legacy.write_text(
        json.dumps(
            {
                "format": "mcrawsome.clip-details",
                "schema_version": 1,
                "source_id": identity,
                "details": {"title": "Legacy"},
            }
        )
    )
    before = legacy.read_bytes()
    assert client.get(f"/api/v1/clips/{identity}/annotations").json()["values"]["title"] == ""
    assert legacy.read_bytes() == before
    assert not source.with_suffix(".meta").exists()


def test_clearing_last_detail_removes_sidecar(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, source = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/annotations"
    record = client.put(url, json={"version": "", "values": {"notes": "Keep"}}).json()
    assert client.put(url, json={"version": record["version"], "values": {}}).status_code == 200
    assert not source.with_suffix(".meta").exists()
