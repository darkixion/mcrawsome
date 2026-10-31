"""Fast Copy through the engine: scan a card, copy with progress, cancel, and refuse a second copy."""

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcrawsome.library import offload

API = "/api/v1"


def _card(root: Path) -> Path:
    folder = root / "card" / "DCIM"
    folder.mkdir(parents=True)
    (folder / "A.mcraw").write_bytes(b"a" * 3000)
    (folder / "A.gcsv").write_bytes(b"g" * 100)
    (folder / "B.mcraw").write_bytes(b"b" * 2000)
    return root / "card"


def _finished(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while True:
        status: dict[str, Any] = client.get(f"{API}/offload").json()
        if not status["running"]:
            return status
        assert time.monotonic() < deadline, "the copy did not finish"
        time.sleep(0.02)


def test_a_scan_lists_the_recordings_and_their_size(client: TestClient, tmp_path: Path) -> None:
    card = _card(tmp_path)
    found = client.post(f"{API}/offload/scan", json={"sources": [str(card)]}).json()
    assert [(r["name"], len(r["files"])) for r in found["recordings"]] == [("A", 2), ("B", 1)]
    assert found["bytes"] == 5100


def test_a_copy_runs_in_the_background_and_reports_each_file(client: TestClient, tmp_path: Path) -> None:
    card, destination = _card(tmp_path), tmp_path / "copied"
    started = client.post(f"{API}/offload", json={"sources": [str(card)], "destination": str(destination)})
    assert started.status_code == 202
    status = _finished(client)
    assert [r["status"] for r in status["results"]] == ["copied"] * 3
    assert status["progress"]["done_bytes"] == 5100
    assert status["destination"] == str(destination)
    assert (destination / "A.gcsv").read_bytes() == b"g" * 100


def test_a_copy_can_be_cancelled_and_a_second_copy_waits(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(offload, "CHUNK", 100)
    gate = threading.Event()
    real = offload._hash_file

    def slow(path: Path) -> str:
        gate.wait(5)
        return real(path)

    monkeypatch.setattr(offload, "_hash_file", slow)
    card, destination = _card(tmp_path), tmp_path / "copied"
    body = {"sources": [str(card)], "destination": str(destination)}
    assert client.post(f"{API}/offload", json=body).status_code == 202
    second = client.post(f"{API}/offload", json=body)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "engine-busy"
    assert client.post(f"{API}/offload/cancel").json()["cancelling"] is True
    gate.set()
    status = _finished(client)
    assert "cancelled" in [r["status"] for r in status["results"]]


@pytest.mark.parametrize("field", ["sources", "destination"])
def test_paths_must_be_absolute(client: TestClient, tmp_path: Path, field: str) -> None:
    body: dict[str, Any] = {"sources": [str(tmp_path)], "destination": str(tmp_path / "x"), field: "relative"}
    if field == "sources":
        body[field] = ["relative"]
    assert client.post(f"{API}/offload", json=body).status_code == 422


def test_a_missing_card_is_reported(client: TestClient, tmp_path: Path) -> None:
    response = client.post(f"{API}/offload/scan", json={"sources": [str(tmp_path / "gone")]})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "input-missing"
