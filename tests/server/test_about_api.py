from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome import __version__
from mcrawsome.packaging import packaging_dir

ROOT = Path(__file__).resolve().parents[2]


def test_about_lists_every_licence(client: TestClient) -> None:
    response = client.get("/api/v1/about")
    assert response.status_code == 200, response.text
    about = response.json()
    assert about["version"] == __version__
    # The installed package's folder: the .deb adds each vendored package's licence to the checkout's.
    folder = packaging_dir() / "LICENCES"
    expected = sorted(p.name for p in folder.iterdir() if p.is_file())
    assert about["licences"] == expected
    assert {p.name for p in (ROOT / "packaging/LICENCES").iterdir()} <= set(expected)
    # The texts are fetched one at a time, on demand: the listing stays small.
    assert len(response.content) < 64_000
    assert about["checks"]


def test_each_licence_text_is_served_on_its_own(client: TestClient) -> None:
    name = client.get("/api/v1/about").json()["licences"][0]
    response = client.get(f"/api/v1/about/licences/{name}")
    assert response.status_code == 200, response.text
    assert response.json() == {"file": name, "text": (ROOT / "packaging/LICENCES" / name).read_text()}


@pytest.mark.parametrize("name", ["missing.txt", "..%2Flinux%2Fmcrawsome.desktop", "%2E%2E"])
def test_only_listed_licences_are_served(client: TestClient, name: str) -> None:
    response = client.get(f"/api/v1/about/licences/{name}")
    assert response.status_code == 404


def test_a_licence_that_is_not_utf8_is_still_served(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "packaging"
    (data / "LICENCES").mkdir(parents=True)
    (data / "LICENCES/Latin-1.txt").write_bytes(b"Copyright \xa9 1999 Fran\xe7ois\n")
    monkeypatch.setattr("mcrawsome.server.app.packaging_dir", lambda: data)
    assert client.get("/api/v1/about").json()["licences"] == ["Latin-1.txt"]
    response = client.get("/api/v1/about/licences/Latin-1.txt")
    assert response.status_code == 200, response.text
    assert response.json()["text"].startswith("Copyright")
    assert "Fran" in response.json()["text"]
