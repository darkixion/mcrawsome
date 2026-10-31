from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome.server import static
from mcrawsome.server.app import create_app
from mcrawsome.server.config import ServerConfig

CSP = (
    "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; style-src 'self' 'unsafe-inline'"
)


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A built interface in tmp_path/web, with a file beside it that must stay out of reach."""
    folder = tmp_path / "web"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html><title>interface</title>")
    (folder / "assets" / "x.js").write_text("export const x = 1;\n")
    (tmp_path / "app.py").write_text("SECRET = 1\n")
    monkeypatch.setattr(static, "WEB", folder)
    return folder


@pytest.fixture
def page(library: Path) -> Iterator[TestClient]:
    app = create_app(ServerConfig(roots=[library]))
    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        yield client


def test_the_page_is_served_with_its_policy(bundle: Path, page: TestClient) -> None:
    response = page.get("/")
    assert response.status_code == 200
    assert response.text == "<!doctype html><title>interface</title>"
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["cache-control"] == "no-cache"
    assert page.get("/index.html").headers["content-security-policy"] == CSP


def test_without_a_bundle_the_page_says_how_to_build_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: TestClient
) -> None:
    monkeypatch.setattr(static, "WEB", tmp_path / "not-built")
    response = page.get("/")
    assert response.status_code == 200
    assert "The interface is not built: run scripts/build_web.py" in response.text
    assert page.get("/assets/x.js").status_code == 404


def test_assets_are_served_with_their_type(bundle: Path, page: TestClient) -> None:
    response = page.get("/assets/x.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.text == "export const x = 1;\n"


def test_a_path_escaping_the_bundle_is_404(bundle: Path, page: TestClient) -> None:
    for path in ("/../app.py", "/%2e%2e/app.py", "/assets/%2e%2e/%2e%2e/app.py", "/assets/..%2f..%2fapp.py"):
        response = page.get(path)
        assert response.status_code == 404, path
        assert "SECRET" not in response.text
        assert response.json()["error"]["code"] == "input-missing"
    assert page.get("/assets").status_code == 404  # a folder is not a file
    assert page.get("/missing.js").status_code == 404


def test_a_path_the_file_system_cannot_take_is_404(bundle: Path, page: TestClient) -> None:
    for path in ("/%00", "/assets/x%00.js"):
        response = page.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "input-missing"


def test_the_api_is_not_shadowed_by_the_page(bundle: Path, page: TestClient) -> None:
    assert page.get("/api/v1/health").status_code == 200
    assert page.get("/api/v1/nothing").json()["error"]["code"] == "input-missing"


def test_desktop_policy_allows_the_pywebview_bridge(bundle: Path, page: TestClient) -> None:
    response = page.get("/", headers={"User-Agent": "mcrawsome-pywebview"})
    assert "script-src 'self' 'unsafe-eval'" in response.headers["content-security-policy"]
    assert page.get("/").headers["content-security-policy"] == CSP
