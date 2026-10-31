"""Native archives are the only Resolve handoff; the old scripted API stays unavailable."""

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from mcrawsome.cli.main import cli
from mcrawsome.server.settings import InterfaceSettings


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/resolve"),
        ("post", "/api/v1/resolve/send"),
        ("post", "/api/v1/resolve/install-script"),
        ("get", "/api/v1/send/pending"),
        ("post", "/api/v1/send/pending/done"),
    ],
)
def test_scripted_routes_are_removed(client: TestClient, method: str, path: str) -> None:
    assert path not in client.get("/api/v1/openapi.json").json()["paths"]
    assert getattr(client, method)(path).status_code in (404, 405)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/plan", {"clips": ["a"], "purpose": "raw-to-resolve"}),
        ("/api/v1/jobs", {"clip_id": "a", "purpose": "raw-to-resolve"}),
        ("/api/v1/jobs", {"clip_id": "a", "purpose": "grading", "send_to": "resolve"}),
    ],
)
def test_scripted_export_requests_are_rejected(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    assert client.post(path, json=payload).status_code in (404, 405)


@pytest.mark.parametrize("old", ["auto", "studio", "menu", "off"])
def test_old_preferences_use_native_sending_without_a_hidden_disable_switch(old: str) -> None:
    settings = InterfaceSettings.model_validate({"resolve_route": old})
    assert settings.resolve_route == "auto"


def test_headless_command_validates_inputs_before_mounting() -> None:
    result = CliRunner().invoke(cli, ["send-to-resolve", "a.mcraw"])
    assert result.exit_code == 2
    assert "does not exist" in result.output
