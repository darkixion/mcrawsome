import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome.io.atomic import write_atomic
from mcrawsome.paths import state_dir
from mcrawsome.recipe.defaults import defaults_path
from mcrawsome.server.settings import settings_path


@pytest.fixture
def local_client(client: TestClient) -> Iterator[TestClient]:
    local = TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 50000))
    try:
        yield local
    finally:
        local.close()


def option(client: TestClient, scope: str) -> dict[str, str]:
    return next(row for row in client.get("/api/v1/reset-options").json() if row["scope"] == scope)


@pytest.mark.parametrize("scope", ["defaults", "preferences", "workspace"])
def test_resets_are_scoped_backed_up_and_restorable_after_restart(
    local_client: TestClient, scope: str
) -> None:
    client = local_client
    if scope == "defaults":
        path = defaults_path()
        original = b"[develop]\nblack_level = 42\n"
    elif scope == "preferences":
        path = settings_path()
        original = b"preview_gpu = false\npreview_cache_mib = 123\n"
    else:
        path = state_dir() / "current-session.json"
        client.put("/api/v1/session", json={"search": "preserve my workspace"})
        original = path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    sentinel = state_dir() / "named-session.json"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"user session")
    before = option(client, scope)
    response = client.post("/api/v1/reset", json={"scope": scope, "version": before["version"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert path.read_bytes() != original
    assert Path(result["backup_path"]).is_file()
    # Recovery inventory is persisted, rather than relying on a transient UI result.
    backups = client.get("/api/v1/reset-backups").json()
    assert any(row["backup"] == result["backup"] for row in backups)
    restored = client.post(
        "/api/v1/reset/restore", json={"backup": result["backup"], "version": result["version"]}
    )
    assert restored.status_code == 200, restored.text
    assert path.read_bytes() == original
    assert sentinel.read_bytes() == b"user session"
    assert not any(row["backup"] == result["backup"] for row in client.get("/api/v1/reset-backups").json())
    if scope == "preferences":
        assert client.get("/api/v1/settings").json()["preview_cache_mib"] == 123
    elif scope == "workspace":
        assert client.get("/api/v1/session").json()["workspace"]["search"] == "preserve my workspace"


def test_reset_refuses_stale_versions_and_restore_preserves_edits(local_client: TestClient) -> None:
    client = local_client
    defaults_path().parent.mkdir(parents=True, exist_ok=True)
    defaults_path().write_bytes(b"[develop]\nblack_level = 42\n")
    stale = option(client, "defaults")
    defaults_path().write_bytes(b"[develop]\nblack_level = 43\n")
    assert (
        client.post("/api/v1/reset", json={"scope": "defaults", "version": stale["version"]}).status_code
        == 409
    )
    before = option(client, "defaults")
    reset = client.post("/api/v1/reset", json={"scope": "defaults", "version": before["version"]}).json()
    defaults_path().write_bytes(b"[develop]\nblack_level = 44\n")
    assert (
        client.post(
            "/api/v1/reset/restore", json={"backup": reset["backup"], "version": reset["version"]}
        ).status_code
        == 409
    )
    assert b"44" in defaults_path().read_bytes()


def test_failed_reset_retains_original_and_recovery_backup(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = local_client
    import mcrawsome.server.routes.reset as module

    target = defaults_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[develop]\nblack_level = 42\n")
    original = write_atomic

    def fail_target(path: Path, content: bytes) -> None:
        if path == target:
            raise OSError("simulated failed reset")
        original(path, content)

    monkeypatch.setattr(module, "write_atomic", fail_target)
    before = option(client, "defaults")
    with pytest.raises(OSError, match="simulated failed reset"):
        client.post("/api/v1/reset", json={"scope": "defaults", "version": before["version"]})
    assert b"42" in target.read_bytes()
    [backup] = (state_dir() / "reset-backups").glob("*.json")
    assert json.loads(backup.read_bytes())["content"] is not None


def test_restore_rejects_arbitrary_file_paths(local_client: TestClient) -> None:
    client = local_client
    assert (
        client.post("/api/v1/reset/restore", json={"backup": "../../recording", "version": ""}).status_code
        == 422
    )
