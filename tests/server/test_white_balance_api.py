import os
import time
from pathlib import Path

from fastapi.testclient import TestClient


def test_picker_api(client: TestClient, white_balance_raw: Path) -> None:
    client.put("/api/v1/settings", json={"folders": [str(white_balance_raw.parent)]})
    request = {"index": 1, "rectangle": [16, 16, 24, 24], "recipe": {"develop": {"lens_shading": 0}}}
    response = client.post(
        "/api/v1/clips/local/white-balance", params={"path": str(white_balance_raw)}, json=request
    )
    assert response.status_code == 200, response.text
    assert 2000 <= response.json()["temperature"] <= 12000
    assert -100 <= response.json()["tint"] <= 100
    request["index"] = -1
    invalid = client.post(
        "/api/v1/clips/local/white-balance", params={"path": str(white_balance_raw)}, json=request
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "recipe-invalid"


def test_picker_checks_library_path(client: TestClient, white_balance_raw: Path) -> None:
    response = client.post(
        "/api/v1/clips/local/white-balance",
        params={"path": str(white_balance_raw)},
        json={"index": 0, "rectangle": [0, 0, 4, 4]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path-not-allowed"


def test_custom_validation_reports_the_camera_limit(client: TestClient, white_balance_raw: Path) -> None:
    client.put("/api/v1/settings", json={"folders": [str(white_balance_raw.parent)]})
    url = "/api/v1/clips/local/white-balance/validate"
    recipe = {"develop": {"white_balance": "custom", "temperature": 2000, "tint": 100}}
    invalid = client.post(url, params={"path": str(white_balance_raw)}, json={"recipe": recipe})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "recipe-invalid"
    assert "outside what this camera" in invalid.json()["error"]["message"]
    recipe["develop"]["temperature"] = 6000
    valid = client.post(url, params={"path": str(white_balance_raw)}, json={"recipe": recipe})
    assert valid.status_code == 200


def test_clip_metadata_reports_median_as_shot(client: TestClient, white_balance_raw: Path) -> None:
    from mcrawsome.colour.white_balance import as_shot
    from mcrawsome.io.clip import Clip

    with Clip(white_balance_raw) as clip:
        kelvin, tint = as_shot(clip)
    os.utime(white_balance_raw, (time.time() - 3600,) * 2)
    client.put("/api/v1/settings", json={"folders": [str(white_balance_raw.parent)]})
    summary = client.get("/api/v1/library").json()["clips"][0]
    assert summary["as_shot_temperature"] == kelvin
    assert summary["as_shot_tint"] == tint
    detail = client.get(f"/api/v1/clips/{summary['id']}").json()
    assert detail["metadata"]["White balance"] == {"temperature": kelvin, "tint": tint}
