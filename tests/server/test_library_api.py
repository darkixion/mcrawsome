"""The library's API: clips in the settings' folders, one clip's details, its thumbnail, and live changes."""

import os
import shutil
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcrawsome.library.scan import Library

API = "/api/v1"
STEP = 41_666_667


def _age(path: Path) -> None:
    when = time.time() - 3600
    os.utime(path, (when, when))


@pytest.fixture
def clips(tmp_path: Path) -> Path:
    folder = tmp_path / "clips"
    folder.mkdir()
    return folder


@pytest.fixture
def clip(make_mcraw: Callable[..., Path]) -> Callable[..., Path]:
    def make(path: Path, first: int = 0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        make_mcraw(path, [first + i * STEP for i in range(3)])
        _age(path)
        return path

    return make


def _choose(client: TestClient, *folders: Path) -> None:
    response = client.put(f"{API}/settings", json={"folders": [str(folder) for folder in folders]})
    assert response.status_code == 200, response.text


def _missing(response: Any) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "clip-missing"


def test_the_library_lists_the_clips_in_the_chosen_folders(
    client: TestClient, clips: Path, clip: Callable[..., Path], tmp_path: Path
) -> None:
    clip(clips / "a.mcraw")
    unplugged = tmp_path / "card"
    unplugged.mkdir()
    _choose(client, clips, unplugged)
    unplugged.rmdir()
    view = client.get(f"{API}/library").json()
    assert [c["name"] for c in view["clips"]] == ["a.mcraw"]
    assert view["clips"][0]["status"] == "ready"
    assert view["missing_folders"] == [str(unplugged)]


def test_a_clips_details_and_a_deleted_clip_is_missing(
    client: TestClient, clips: Path, clip: Callable[..., Path]
) -> None:
    path = clip(clips / "a.mcraw")
    (clips / "a.mcrawsome.toml").write_text('look = "bond"\n')
    _choose(client, clips)
    [listed] = client.get(f"{API}/library").json()["clips"]
    details = client.get(f"{API}/clips/{listed['id']}")
    assert details.status_code == 200, details.text
    body = details.json()
    assert body["path"] == str(path)
    assert body["recipe"]["values"] == {"look": "bond"}
    assert body["metadata"]["First frame"]["width"] == body["width"]
    assert "blackLevel" in body["metadata"]["Container"]
    assert (body["gyro"], body["dropped_frames"], body["filled_frames"]) == (False, 0, 0)
    path.unlink()
    _missing(client.get(f"{API}/clips/{listed['id']}"))
    _missing(client.get(f"{API}/clips/{listed['id']}/thumbnail"))
    _missing(client.get(f"{API}/clips/0123456789abcdef"))


def test_a_clip_at_two_paths_is_reported_at_its_first_path_with_that_paths_recipe(
    client: TestClient, clips: Path, clip: Callable[..., Path]
) -> None:
    second = clip(clips / "b.mcraw")
    first = clips / "a.mcraw"
    shutil.copy2(second, first)
    (clips / "a.mcrawsome.toml").write_text('look = "bond"\n')
    (clips / "b.mcrawsome.toml").write_text('look = "joker"\n')
    _choose(client, clips)
    [listed] = client.get(f"{API}/library").json()["clips"]
    assert listed["paths"] == [str(first), str(second)]
    body = client.get(f"{API}/clips/{listed['id']}").json()
    assert body["path"] == str(first)
    assert body["recipe"]["values"] == {"look": "bond"}
    assert body["metadata"]["First frame"]["width"] == body["width"]
    assert "blackLevel" in body["metadata"]["Container"]
    first.unlink()
    body = client.get(f"{API}/clips/{listed['id']}").json()  # the id stays, at the remaining path
    assert (body["path"], body["recipe"]["values"]) == (str(second), {"look": "joker"})


def test_the_thumbnail_is_a_jpeg(client: TestClient, clips: Path, clip: Callable[..., Path]) -> None:
    clip(clips / "a.mcraw")
    _choose(client, clips)
    [listed] = client.get(f"{API}/library").json()["clips"]
    response = client.get(f"{API}/clips/{listed['id']}/thumbnail")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"


def test_a_card_skim_is_ten_jpegs(client: TestClient, clips: Path, clip: Callable[..., Path]) -> None:
    import base64

    clip(clips / "a.mcraw")
    _choose(client, clips)
    [listed] = client.get(f"{API}/library").json()["clips"]
    response = client.get(f"{API}/clips/{listed['id']}/skim")
    assert response.status_code == 200, response.text
    frames = response.json()["frames"]
    assert len(frames) == 10
    assert all(base64.b64decode(frame)[:2] == b"\xff\xd8" for frame in frames)


def test_an_unreadable_clip_has_no_skim(client: TestClient, clips: Path) -> None:
    path = clips / "broken.mcraw"
    path.write_bytes(b"synthetic broken clip")
    _age(path)
    _choose(client, clips)
    [listed] = client.get(f"{API}/library").json()["clips"]
    response = client.get(f"{API}/clips/{listed['id']}/skim")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input-unreadable"


def test_a_clip_removed_between_lookup_and_open_is_missing(
    client: TestClient, clips: Path, clip: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = clip(clips / "a.mcraw")
    _choose(client, clips)
    [listed] = client.get(f"{API}/library").json()["clips"]
    real = Library.find

    def find_then_lose(self: Library, clip_id: str) -> object:
        found = real(self, clip_id)
        path.unlink(missing_ok=True)
        return found

    monkeypatch.setattr(Library, "find", find_then_lose)
    _missing(client.get(f"{API}/clips/{listed['id']}/thumbnail"))
    clip(clips / "a.mcraw")
    _missing(client.get(f"{API}/clips/{listed['id']}"))


def test_a_symlink_to_a_clip_outside_the_roots_is_not_listed(
    client: TestClient, clips: Path, clip: Callable[..., Path], tmp_path: Path
) -> None:
    outside = clip(tmp_path / "private" / "secret.mcraw")
    (clips / "link.mcraw").symlink_to(outside)
    _choose(client, clips)
    assert client.get(f"{API}/library").json()["clips"] == []


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def test_unreadable_thumbnail_has_a_coded_client_error(client: TestClient, clips: Path) -> None:
    path = clips / "broken.mcraw"
    path.write_bytes(b"synthetic broken clip")
    _age(path)
    _choose(client, clips)
    [listed] = client.get(f"{API}/library").json()["clips"]
    assert listed["status"] == "unreadable"
    response = client.get(f"{API}/clips/{listed['id']}/thumbnail")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input-unreadable"
