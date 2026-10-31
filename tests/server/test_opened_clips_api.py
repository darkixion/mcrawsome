"""Temporary references never scan neighbouring files or alter watched library folders."""

import os
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def opened_client(client: TestClient) -> Iterator[TestClient]:
    with closing(
        TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 1234))
    ) as local:
        yield local


def test_opened_files_are_separate_and_work_with_existing_clip_routes(
    opened_client: TestClient, make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    client = opened_client
    folder = tmp_path / "opened"
    folder.mkdir()
    source = make_mcraw(folder / "chosen.mcraw", [0, 41666667, 83333334])
    neighbour = make_mcraw(folder / "other.mcraw", [0, 41666667])
    for path in [source, neighbour]:
        os.utime(path, (time.time() - 60,) * 2)
    before = source.read_bytes()
    settings = client.get("/api/v1/settings").json()
    response = client.post("/api/v1/opened-clips", json={"paths": [str(source)]})
    assert response.status_code == 200, response.text
    [clip] = response.json()["clips"]
    assert clip["name"] == "chosen.mcraw"
    assert clip["status"] == "ready"
    assert client.get("/api/v1/settings").json() == settings
    assert client.get("/api/v1/library").json()["clips"] == []
    assert client.get(f"/api/v1/clips/{clip['id']}").json()["path"] == str(source)
    assert client.get(f"/api/v1/clips/{clip['id']}/thumbnail").status_code == 200
    assert len(client.post("/api/v1/opened-clips", json={"paths": [str(source)]}).json()["clips"]) == 1
    assert client.get("/api/v1/opened-clips").json()["clips"] == response.json()["clips"]
    assert client.get(f"/api/v1/clips/{clip['id']}/recipe").status_code == 200
    assert client.get(f"/api/v1/clips/{clip['id']}/annotations").status_code == 200
    closed = client.delete("/api/v1/opened-clips")
    assert closed.status_code == 200
    assert closed.json()["clips"] == []
    assert source.read_bytes() == before
    assert client.get("/api/v1/settings").json() == settings


def test_opened_input_validation_is_atomic_and_local_only(
    opened_client: TestClient, tiny_raw: Path, tmp_path: Path
) -> None:
    client = opened_client
    bad = tmp_path / "unsupported.txt"
    bad.write_text("not media")
    response = client.post("/api/v1/opened-clips", json={"paths": [str(tiny_raw), str(bad)]})
    assert response.status_code == 422
    assert client.get("/api/v1/opened-clips").json()["clips"] == []
    response = client.post("/api/v1/opened-clips", json={"paths": [str(tmp_path)]})
    assert response.status_code == 422
    with closing(
        TestClient(client.app, base_url="http://127.0.0.1:8642", client=("192.0.2.10", 1234))
    ) as remote:
        assert remote.post("/api/v1/opened-clips", json={"paths": [str(tiny_raw)]}).status_code == 403
        assert remote.post("/api/v1/opened-clips/remove", json={"paths": [str(tiny_raw)]}).status_code == 403


@pytest.mark.parametrize("suffix", [".mp4", ".mov", ".mkv"])
def test_nonlog_video_is_rejected_before_opened_references_change(
    opened_client: TestClient, tiny_raw: Path, tmp_path: Path, suffix: str
) -> None:
    import subprocess

    from conftest import generate_synthetic_video

    from mcrawsome.output.ffmpeg import find_ffmpeg

    original = generate_synthetic_video(tmp_path)
    candidate = tmp_path / f"renamed_Sony S-Log3_S-Gamut3C{suffix}"
    subprocess.run(
        [
            str(find_ffmpeg()[0]),
            "-v",
            "error",
            "-i",
            str(original),
            "-c",
            "copy",
            "-map_metadata",
            "-1",
            str(candidate),
        ],
        check=True,
    )
    reply = opened_client.post("/api/v1/opened-clips", json={"paths": [str(tiny_raw), str(candidate)]})
    assert reply.status_code == 422
    assert "embedded metadata" in reply.json()["error"]["message"]
    assert opened_client.get("/api/v1/opened-clips").json()["clips"] == []


@pytest.mark.parametrize("suffix", [".mp4", ".mov", ".mkv"])
def test_real_log_metadata_is_read_across_containers(
    opened_client: TestClient, tmp_path: Path, suffix: str
) -> None:
    import subprocess

    from conftest import generate_synthetic_video

    from mcrawsome.output.ffmpeg import find_ffmpeg

    original = generate_synthetic_video(tmp_path)
    candidate = tmp_path / f"plain_name{suffix}"
    options = ["-movflags", "use_metadata_tags"] if suffix != ".mkv" else []
    subprocess.run(
        [
            str(find_ffmpeg()[0]),
            "-v",
            "error",
            "-i",
            str(original),
            "-c",
            "copy",
            "-map_metadata",
            "-1",
            "-metadata",
            "transfer_function=S-Log3",
            *options,
            str(candidate),
        ],
        check=True,
    )
    import os
    import time

    os.utime(candidate, (time.time() - 60,) * 2)
    reply = opened_client.post("/api/v1/opened-clips", json={"paths": [str(candidate)]})
    assert reply.status_code == 200, reply.text
    [clip] = reply.json()["clips"]
    assert clip["kind"] == "log"
    assert clip["status"] == "ready"


def test_remove_selected_references_preserves_remaining_clips_and_files(
    opened_client: TestClient, make_mcraw: Callable[..., Path], tmp_path: Path
) -> None:
    sources = [
        make_mcraw(tmp_path / f"{name}.mcraw", [index * 100000000, index * 100000000 + 41666667])
        for index, name in enumerate(("a", "b", "c"))
    ]
    for path in sources:
        os.utime(path, (time.time() - 60,) * 2)
    original = [path.read_bytes() for path in sources]
    client = opened_client
    assert client.post("/api/v1/opened-clips", json={"paths": list(map(str, sources))}).status_code == 200
    response = client.post("/api/v1/opened-clips/remove", json={"paths": list(map(str, sources[:2]))})
    assert response.status_code == 200, response.text
    assert [clip["path"] for clip in response.json()["clips"]] == [str(sources[2])]
    assert client.get("/api/v1/opened-clips").json() == response.json()
    assert client.get("/api/v1/session").json()["workspace"]["paths"] == [str(sources[2])]
    assert [path.read_bytes() for path in sources] == original
    reopened = client.post("/api/v1/opened-clips", json={"paths": [str(sources[0])]})
    assert {clip["path"] for clip in reopened.json()["clips"]} == set(map(str, (sources[0], sources[2])))


def test_an_opened_clip_is_found_without_rescanning_the_folders(
    opened_client: TestClient,
    make_mcraw: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.library.scan import Library

    client = opened_client
    source = make_mcraw(tmp_path / "chosen.mcraw", [0, 41666667, 83333334])
    os.utime(source, (time.time() - 60,) * 2)
    [clip] = client.post("/api/v1/opened-clips", json={"paths": [str(source)]}).json()["clips"]
    rescans: list[object] = []
    original = Library.refresh

    def counting(self: Library, *args: object, **kwargs: object) -> object:
        rescans.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Library, "refresh", counting)
    for suffix in ("", "/recipe", "/annotations", "/thumbnail"):
        assert client.get(f"/api/v1/clips/{clip['id']}{suffix}").status_code == 200
    assert rescans == []
    source.unlink()  # gone: the lookup looks again, and says so
    assert client.get(f"/api/v1/clips/{clip['id']}").status_code == 404
