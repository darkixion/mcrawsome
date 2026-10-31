"""Clip Recipe edits and background analysis use the library's ids."""

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

API = "/api/v1"


def listed_clip(client: TestClient, library: Path, make_mcraw: Callable[..., Path]) -> tuple[str, Path]:
    path = make_mcraw(library / "a.mcraw", [0, 41_666_667, 83_333_334])
    os.utime(path, (time.time() - 60,) * 2)
    assert client.put(f"{API}/settings", json={"folders": [str(library)]}).status_code == 200
    [clip] = client.get(f"{API}/library").json()["clips"]
    return str(clip["id"]), path


def test_recipe_versions_and_validation(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    clip_id, _ = listed_clip(client, library, make_mcraw)
    url = f"{API}/clips/{clip_id}/recipe"
    response = client.get(url)
    assert response.status_code == 200
    body = response.json()
    assert body["layer"]["values"] == {}
    assert any(f["name"] == "develop.exposure" for f in body["fields"])
    update = {"values": {"develop": {"exposure": 1.0}}, "version": body["layer"]["version"]}
    saved = client.put(url, json=update)
    assert saved.status_code == 200, saved.text
    assert client.get(url).json()["resolved"]["develop"]["exposure"] == 1.0
    stale = client.put(url, json=update)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "recipe-changed"
    assert stale.json()["layer"] == saved.json()
    invalid = client.put(
        url, json={"values": {"develop": {"exposure": 100}}, "version": saved.json()["version"]}
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["field"] == "develop.exposure"
    assert client.get(url).json()["layer"] == saved.json()


def test_clip_routes_reject_unknown_and_vanished_ids(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    clip_id, path = listed_clip(client, library, make_mcraw)
    path.unlink()
    for suffix in ("recipe", "analysis"):
        for missing in (clip_id, "unknown"):
            response = client.get(f"{API}/clips/{missing}/{suffix}")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "clip-missing"


def test_trash_moves_the_recording_and_its_companions(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import closing

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    clip_id, path = listed_clip(client, library, make_mcraw)
    path.with_suffix(".gcsv").write_text("t,gx,gy,gz\n")
    url = f"{API}/clips/trash"
    remote = client.post(url, json={"clip_ids": [clip_id]})
    assert remote.status_code == 403, remote.text
    assert path.exists()
    with closing(TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 9))) as local:
        response = local.post(url, json={"clip_ids": [clip_id]})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "trashed": [str(path), str(path.with_suffix(".gcsv"))],
            "unmounted": [],
            "kept_parts": [],
        }
        assert not path.exists()
        assert not path.with_suffix(".gcsv").exists()
        files = tmp_path / "data/Trash/files"
        assert sorted(item.name for item in files.iterdir()) == ["a.gcsv", "a.mcraw"]
        assert local.post(url, json={"clip_ids": [clip_id]}).status_code == 404


def test_trash_refuses_media_an_editor_has_open(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import closing

    from mcrawsome.mount.manager import MountedSequence, MountListing

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    clip_id, path = listed_clip(client, library, make_mcraw)
    busy = MountedSequence(
        id="0" * 24, source=path, path=tmp_path / "m", stamp=(0, 0, 0, 0), active=True, in_use=True
    )
    from mcrawsome.mount import manager as module

    listing = MountListing(available=True, reason="", mounts=[busy])
    monkeypatch.setattr(module.MountManager, "listing", lambda self: listing)
    unmounted: list[str] = []
    monkeypatch.setattr(module.MountManager, "unmount", lambda self, identity: unmounted.append(identity))
    with closing(TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 9))) as local:
        response = local.post(f"{API}/clips/trash", json={"clip_ids": [clip_id]})
    assert response.status_code == 409, response.text
    assert "close it there first" in response.json()["error"]["message"]
    assert path.exists()
    assert unmounted == []


def test_analysis_is_cached_and_published(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.server.routes.clips import AnalysisHost

    clip_id, _ = listed_clip(client, library, make_mcraw)
    assert isinstance(client.app, FastAPI)
    host: AnalysisHost = client.app.state.analysis
    changes = host.subscribe()
    try:
        url = f"{API}/clips/{clip_id}/analysis"
        assert client.get(url).status_code == 202
        event = changes.get(timeout=10)
        assert event["id"] == clip_id
        assert "report" in event, event
        assert client.get(url).json() == event["report"]
        assert list((library.parent / "cache" / "analysis").glob("*.json"))

        def unexpected(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("cached analysis was recomputed")

        monkeypatch.setattr("mcrawsome.server.routes.clips.analyse_report", unexpected)
        assert client.get(url).status_code == 200
        recipe_url = f"{API}/clips/{clip_id}/recipe"
        version = client.get(recipe_url).json()["layer"]["version"]
        client.put(recipe_url, json={"values": {"output": {"crf": 20}}, "version": version})
        assert client.get(url).status_code == 200
    finally:
        host.unsubscribe(changes)


def test_analysis_suggests_the_automatic_exposure_while_exposure_is_set(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    from mcrawsome.server.routes.clips import AnalysisHost

    clip_id, _ = listed_clip(client, library, make_mcraw)
    assert isinstance(client.app, FastAPI)
    host: AnalysisHost = client.app.state.analysis
    url = f"{API}/clips/{clip_id}/analysis"
    recipe_url = f"{API}/clips/{clip_id}/recipe"
    changes = host.subscribe()
    try:
        assert client.get(url).status_code == 202
        automatic = changes.get(timeout=30)["report"]
        suggested = automatic["suggested_exposure"]
        assert isinstance(suggested, float)
        assert automatic["Exposure"]["Exposure"] == f"{suggested:+.1f} stops (automatic)"
        version = client.get(recipe_url).json()["layer"]["version"]
        chosen = suggested + 1.5
        body = {"values": {"develop": {"exposure": chosen}}, "version": version}
        assert client.put(recipe_url, json=body).status_code == 200
        assert client.get(url).status_code == 202
        report = changes.get(timeout=30)["report"]
        assert report["Exposure"]["Exposure"] == f"{chosen:+.1f} stops (set)"
        assert report["suggested_exposure"] == suggested
        assert client.get(url).json() == report
    finally:
        host.unsubscribe(changes)


def test_analysis_stream_and_single_worker(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import json
    import threading

    from fastapi import Request
    from starlette.types import Message

    from mcrawsome.analysis.report import Report
    from mcrawsome.library.scan import Library
    from mcrawsome.library.watch import LibraryWatcher
    from mcrawsome.server.app import _events
    from mcrawsome.server.routes.clips import AnalysisHost

    clip_id, _ = listed_clip(client, library, make_mcraw)
    assert isinstance(client.app, FastAPI)
    host: AnalysisHost = client.app.state.analysis
    entered, release = threading.Event(), threading.Event()
    calls: list[float | str] = []

    def measure(path: Path, recipe: Any, workers: int, staging: Path) -> Report:
        calls.append(recipe.develop.exposure)
        assert workers <= 4
        entered.set()
        assert release.wait(10)
        return Report([("Exposure", [("Exposure", str(recipe.develop.exposure))])])

    monkeypatch.setattr("mcrawsome.server.routes.clips.analyse_report", measure)
    watcher = LibraryWatcher(Library(), lambda: [library])
    stopping = threading.Event()

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def follow() -> None:
        stream = _events(watcher, Request({"type": "http"}, receive), stopping, host)
        try:
            assert await anext(stream) == ": connected\n\n"
            url = f"{API}/clips/{clip_id}/analysis"
            assert client.get(url).status_code == 202
            assert entered.wait(10)
            assert client.get(url).status_code == 202  # the same computation
            recipe_url = f"{API}/clips/{clip_id}/recipe"
            version = client.get(recipe_url).json()["layer"]["version"]
            assert (
                client.put(
                    recipe_url, json={"values": {"develop": {"exposure": 1}}, "version": version}
                ).status_code
                == 200
            )
            assert client.get(url).status_code == 202  # queued behind the first Recipe
            assert len(calls) == 1
            release.set()
            for _ in range(2):
                event = await asyncio.wait_for(anext(stream), 10)
                assert event.startswith("event: analysis\n")
                assert json.loads(event.split("data: ")[1])["id"] == clip_id
            assert len(calls) == 2
            assert client.get(url).status_code == 200
        finally:
            release.set()
            await stream.aclose()

    asyncio.run(follow())
    assert not host._subscribers


def test_invalid_and_cleared_recipe_fields(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    clip_id, _ = listed_clip(client, library, make_mcraw)
    url = f"{API}/clips/{clip_id}/recipe"
    for values, field in (
        ({"develop": {"exposur": 1}}, "develop.exposur"),
        ({"develop": {"exposure": None}}, "develop.exposure"),
    ):
        response = client.put(url, json={"values": values, "version": ""})
        assert response.status_code == 422, response.text
        assert response.json()["error"]["field"] == field


def test_failed_analysis_can_be_retried(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.server.routes.clips import AnalysisHost

    clip_id, _ = listed_clip(client, library, make_mcraw)
    assert isinstance(client.app, FastAPI)
    host: AnalysisHost = client.app.state.analysis
    changes = host.subscribe()
    calls = 0

    def failing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ValueError("temporary analysis failure")

    monkeypatch.setattr("mcrawsome.server.routes.clips.analyse_report", failing)
    try:
        url = f"{API}/clips/{clip_id}/analysis"
        assert client.get(url).status_code == 202
        assert "error" in changes.get(timeout=10)
        assert client.get(url).json()["error"]["code"] == "input-unreadable"
        assert client.get(url).status_code == 202
        assert "error" in changes.get(timeout=10)
        assert calls == 2
    finally:
        host.unsubscribe(changes)


@pytest.mark.parametrize("field", ["start", "duration"])
@pytest.mark.parametrize("text", ["9" * 310, "9" * 310 + ":00", "9" * 310 + ":00:00"])
def test_large_trim_is_a_field_error(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], field: str, text: str
) -> None:
    clip_id, path = listed_clip(client, library, make_mcraw)
    url = f"{API}/clips/{clip_id}/recipe"
    response = client.put(url, json={"values": {"trim": {field: text}}, "version": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "recipe-invalid"
    assert response.json()["error"]["field"] == f"trim.{field}"
    path.with_suffix(".mcrawsome.toml").write_text(f'[trim]\n{field} = "{text}"\n')
    body = client.get(url).json()
    assert f"trim.{field}" in body["layer"]["error"]


def test_analysis_tracks_version_and_current_workers(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis.report import Report
    from mcrawsome.server.routes import clips

    clip_id, _ = listed_clip(client, library, make_mcraw)
    assert isinstance(client.app, FastAPI)
    host = client.app.state.analysis
    changes = host.subscribe()
    workers_used: list[int] = []

    def measure(path: Path, recipe: Any, workers: int, staging: Path) -> Report:
        workers_used.append(workers)
        return Report([])

    monkeypatch.setattr(clips, "analyse_report", measure)
    url = f"{API}/clips/{clip_id}/analysis"
    try:
        for count in (1, 2):
            monkeypatch.setattr(clips, "__version__", f"version-{count}")
            client.put(f"{API}/settings", json={"folders": [str(library)], "workers": count})
            assert client.get(url).status_code == 202
            assert "report" in changes.get(timeout=10)
            assert workers_used[-1] == count
        monkeypatch.setattr(clips, "__version__", "new-version", raising=False)
        assert client.get(url).status_code == 202
        assert "report" in changes.get(timeout=10)
    finally:
        host.unsubscribe(changes)


def test_analysis_cache_is_pruned_by_count_and_bytes(tmp_path: Path) -> None:
    from mcrawsome.server.routes.clips import prune_analysis

    for index in range(5):
        path = tmp_path / f"{index}.json"
        path.write_bytes(b"x" * 10)
        os.utime(path, (index + 1, index + 1))
    prune_analysis(tmp_path, max_files=3, max_bytes=25)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.json", "4.json"]
