from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome.mount.sequence import Sequence
from mcrawsome.paths import cache_dir
from mcrawsome.recipe.raw import RawCalibration


@pytest.mark.parametrize(
    ("kind", "suffix"), [("analysis", ".json"), ("optical-tracks", ".npz"), ("exposure", ".json")]
)
def test_cache_usage_and_scoped_clear_preserve_work_readers_and_user_data(
    client: TestClient, tmp_path: Path, kind: str, suffix: str
) -> None:
    folder = cache_dir() / kind
    folder.mkdir(parents=True)
    entry = folder / f"completed{suffix}"
    entry.write_bytes(b"cached-data")
    staged = folder / f"writing{suffix}.part"
    staged.write_bytes(b"pending")
    work = folder / "work"
    work.mkdir()
    dependent = work / f"retained{suffix}"
    dependent.write_bytes(b"work")
    external = tmp_path / f"recording{suffix}"
    external.write_bytes(b"original")
    (folder / f"linked{suffix}").symlink_to(external)
    rows = client.get("/api/v1/cache").json()
    before = next(row for row in rows if row["kind"] == kind)
    assert before["bytes"] == len(b"cached-data")
    assert before["files"] == 1
    with entry.open("rb") as reader:
        response = client.post("/api/v1/cache/clear", json={"kind": kind})
        assert response.status_code == 200
        assert response.json()["bytes"] == 0
        assert reader.read() == b"cached-data"
    assert staged.read_bytes() == b"pending"
    assert dependent.read_bytes() == b"work"
    assert external.read_bytes() == b"original"


@pytest.mark.parametrize("kind", ["audio", "preview", "thumbs", "../mounts", "profiles", "sessions"])
def test_clear_refuses_nonreclaimable_categories(client: TestClient, kind: str) -> None:
    assert client.post("/api/v1/cache/clear", json={"kind": kind}).status_code == 422


def test_cache_clear_does_not_change_a_sequence_with_loaded_exposure_measurements(
    client: TestClient, tiny_raw: Path
) -> None:
    with Sequence(tiny_raw, raw_settings=RawCalibration(mount_normalise_exposure="picture")) as sequence:
        name = sequence.names()[1]
        before = sequence.read(name, 0, sequence.size(name))
        assert client.post("/api/v1/cache/clear", json={"kind": "exposure"}).status_code == 200
        sequence.pixels.clear()
        assert sequence.read(name, 0, sequence.size(name)) == before


def test_usage_reports_session_plan_files_without_following_symlinks(
    client: TestClient, tmp_path: Path
) -> None:
    folder = cache_dir() / "preview" / "active"
    folder.mkdir(parents=True)
    (folder / "plan.npz").write_bytes(b"plan")
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.npz").write_bytes(b"private")
    (folder / "linked").symlink_to(external, target_is_directory=True)
    rows = client.get("/api/v1/cache").json()
    plans = next(row for row in rows if row["kind"] == "preview")
    assert plans["bytes"] == 4
    assert plans["reclaimable"] is False


def test_preview_cache_without_an_open_worker_is_explicitly_inactive(client: TestClient) -> None:
    for response in (client.get("/api/v1/cache/preview"), client.post("/api/v1/cache/preview/clear")):
        assert response.status_code == 200
        assert response.json() == {
            "active": False,
            "bytes": 0,
            "linear_bytes": 0,
            "jpeg_bytes": 0,
            "limit_bytes": 0,
            "entries": 0,
        }


@pytest.mark.parametrize("retained", [False, True])
def test_live_clear_preserves_websocket_audio_paths_and_mounted_bytes(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], retained: bool
) -> None:
    from urllib.parse import urlencode

    from mcrawsome.library.scan import clip_id
    from mcrawsome.mount.manager import MountManager, availability

    if retained and not availability()[0]:
        pytest.skip("actual retained-media check needs FUSE")

    source = make_mcraw(library / "cache.mcraw", [i * 41666667 for i in range(6)])
    identity = clip_id(source)
    protected = []
    for folder, name in (("audio", "playing.wav"), ("thumbs", "showing.jpg"), ("preview", "plan.npz")):
        path = cache_dir() / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"active reader")
        protected.append(path)
    with (
        ExitStack() as cleanup,
        Sequence(source) as mounted,
        client.websocket_connect(
            f"ws://127.0.0.1:8642/api/v1/clips/{identity}/preview?{urlencode({'path': str(source)})}",
            headers={"origin": "http://127.0.0.1:8642"},
        ) as ws,
        protected[0].open("rb") as reader,
    ):
        assert ws.receive_json()["kind"] == "opened"
        ws.send_json(
            {
                "seq": 1,
                "op": "recipe",
                "index": 1,
                "recipe": {"develop": {"exposure": 0}},
                "fast": True,
                "playback": True,
            }
        )
        header = ws.receive_json()
        assert header["kind"] == "draft", header
        assert ws.receive_bytes().startswith(b"\xff\xd8")
        dependencies = []
        manager = MountManager()
        if retained:
            for size in ("full", "quarter"):
                dependency = manager.mount(
                    source, raw_settings=RawCalibration.model_validate({"mount_proxy": size})
                )
                cleanup.callback(manager.unmount, dependency.id)
                dependencies.append(dependency)
            manager.retain([m.id for m in dependencies])
        records = [manager.root / m.id / "sequence.json" for m in dependencies]
        saved_records = [path.read_bytes() for path in records]
        served_files = [m.path / (source.stem + "-000000.dng") for m in dependencies]
        saved_media = [path.read_bytes() for path in served_files]
        before = client.get("/api/v1/cache/preview").json()
        assert before["active"]
        assert before["bytes"] > 0
        name = mounted.names()[0]
        dng = bytes(mounted.read(name, 0, mounted.size(name)))
        audio_response = client.get(f"/api/v1/clips/{identity}/audio", params={"path": str(source)})
        assert audio_response.status_code == 200
        audio_before = audio_response.content
        assert audio_before.startswith(b"RIFF")
        response = client.post("/api/v1/cache/preview/clear")
        assert response.status_code == 200
        assert response.json()["bytes"] == 0
        assert response.json()["limit_bytes"] == before["limit_bytes"]
        assert reader.read() == b"active reader"
        assert all(path.read_bytes() == b"active reader" for path in protected)
        assert bytes(mounted.read(name, 0, mounted.size(name))) == dng
        assert (
            client.get(f"/api/v1/clips/{identity}/audio", params={"path": str(source)}).content
            == audio_before
        )
        assert [path.read_bytes() for path in records] == saved_records
        assert [path.read_bytes() for path in served_files] == saved_media
        ws.send_json({"seq": 2, "op": "frame", "index": 2, "fast": True, "playback": True})
        header = ws.receive_json()
        assert header["kind"] == "draft"
        assert header["seq"] == 2
        assert ws.receive_bytes().startswith(b"\xff\xd8")
