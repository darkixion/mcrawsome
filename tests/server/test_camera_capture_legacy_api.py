from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_clips_api import listed_clip

from mcrawsome.camera.profile import (
    CameraProfile,
    all_profiles,
    find_profile,
    load_profile,
    save_profile,
    user_dir,
)
from mcrawsome.pipeline.lens_calibrate import LensCaptureResult


@pytest.fixture
def client(client: TestClient) -> Iterator[TestClient]:
    local = TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 50000))
    try:
        yield local
    finally:
        local.close()


@pytest.fixture(autouse=True)
def clear_profiles() -> None:
    all_profiles.cache_clear()


def profile() -> dict[str, Any]:
    return {
        "name": "screen_test",
        "match": {"device_model": "Pixel 8", "width": 2048, "height": 1536},
        "focal_px": 1450,
        "lens_calibration": {
            "model": "opencv_standard",
            "width": 2048,
            "height": 1536,
            "original_width": 2048,
            "original_height": 1536,
            "camera_matrix": [[1450, 0, 1024], [0, 1450, 768], [0, 0, 1]],
            "coefficients": [-0.1, 0.02, 0, 0, 0],
            "image_count": 30,
            "rms_error": 0.3,
        },
    }


def test_camera_profile_install_and_conflicts(client: TestClient) -> None:
    data = profile()
    response = client.post("/api/v1/camera-capture", json={"profile": data})
    assert response.status_code == 200, response.text
    saved = load_profile(user_dir() / "screen_test.toml")
    assert saved.lens_calibration is not None
    assert saved.lens_calibration.image_count == 30
    listing = client.get("/api/v1/camera-capture").json()["profiles"]
    assert any(entry["profile"]["name"] == "screen_test" and entry["user"] for entry in listing)
    assert client.post("/api/v1/camera-capture", json={"profile": data}).status_code == 409
    assert load_profile(user_dir() / "screen_test.toml") == saved


def test_deleting_a_profile_updates_matching_and_allows_reusing_its_name(client: TestClient) -> None:
    data = profile()
    assert client.post("/api/v1/camera-capture", json={"profile": data}).status_code == 200
    found = find_profile("Pixel 8", None, 2048, 1536, 24)
    assert found is not None
    assert found.name == "screen_test"
    response = client.delete("/api/v1/camera-capture/screen_test")
    assert response.status_code == 200, response.text
    assert not any(entry["profile"]["name"] == "screen_test" for entry in response.json()["profiles"])
    assert not (user_dir() / "screen_test.toml").exists()
    assert find_profile("Pixel 8", None, 2048, 1536, 24) is None
    assert client.post("/api/v1/camera-capture", json={"profile": data}).status_code == 200


def test_deletion_removes_only_the_user_override_of_a_builtin(client: TestClient) -> None:
    name = "pixel_10_pro_xl_4k"
    save_profile(
        CameraProfile(
            name=name,
            match={"device_model": "Pixel 10 Pro XL", "width": 3840, "height": 2160},
            readout_ms=12,
        )
    )
    found = find_profile("Pixel 10 Pro XL", None, 3840, 2160, 24)
    assert found is not None
    assert found.readout_ms == 12
    response = client.delete(f"/api/v1/camera-capture/{name}")
    assert response.status_code == 200, response.text
    remaining = [entry for entry in response.json()["profiles"] if entry["profile"]["name"] == name]
    assert len(remaining) == 1
    assert not remaining[0]["user"]
    fallback = find_profile("Pixel 10 Pro XL", None, 3840, 2160, 24)
    assert fallback is not None
    assert fallback.readout_ms == 18
    assert client.delete(f"/api/v1/camera-capture/{name}").status_code == 403


@pytest.mark.parametrize(("name", "status"), [("absent", 404), ("bad-name", 422)])
def test_deleting_an_unknown_or_invalid_profile_is_refused(
    client: TestClient, name: str, status: int
) -> None:
    response = client.delete(f"/api/v1/camera-capture/{name}")
    assert response.status_code == status, response.text


def test_delete_failure_is_reported_without_removing_the_profile(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = profile()
    assert client.post("/api/v1/camera-capture", json={"profile": data}).status_code == 200

    def refused(name: str) -> None:
        raise PermissionError("read-only directory")

    monkeypatch.setattr("mcrawsome.server.routes.cameras.delete_profile", refused)
    response = client.delete("/api/v1/camera-capture/screen_test")
    assert response.status_code == 422
    assert "read-only directory" in response.json()["error"]["message"]
    assert (user_dir() / "screen_test.toml").is_file()


@pytest.mark.parametrize(("field", "value"), [("rms_error", 4), ("image_count", 2)])
def test_bad_measurements_cannot_be_installed(client: TestClient, field: str, value: int) -> None:
    data = profile()
    data["lens_calibration"][field] = value
    response = client.post("/api/v1/camera-capture", json={"profile": data})
    assert response.status_code == 422
    assert not (user_dir() / "screen_test.toml").exists()


def test_analysis_uses_registered_recordings_and_reports_capture_errors(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, path = listed_clip(client, library, make_mcraw)
    calls = []

    def analyse(source: Path, *args: object, **kwargs: object) -> LensCaptureResult:
        calls.append(source)
        raise ValueError("Keep all checkerboard corners visible")

    monkeypatch.setattr("mcrawsome.server.routes.cameras.analyse_camera", analyse)
    body = {"clip_id": identity, "name": "screen_test"}
    response = client.post("/api/v1/camera-capture/analyse", json=body)
    assert response.status_code == 422, response.text
    assert "corners visible" in response.json()["error"]["message"]
    assert calls == [path]
    assert (
        client.post("/api/v1/camera-capture/analyse", json=body | {"clip_id": "missing"}).status_code == 404
    )
    assert client.post("/api/v1/camera-capture/analyse", json=body | {"name": "../escape"}).status_code == 422


def test_successful_analysis_returns_json_without_installing(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, _ = listed_clip(client, library, make_mcraw)
    checked = CameraProfile.model_validate(profile())
    result = LensCaptureResult(
        profile=checked, document={"name": "screen_test", "frame_readout_time": None}, passed=True, issues=[]
    )
    monkeypatch.setattr("mcrawsome.server.routes.cameras.analyse_camera", lambda *args, **kwargs: result)
    response = client.post(
        "/api/v1/camera-capture/analyse", json={"clip_id": identity, "name": "screen_test"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["document"]["frame_readout_time"] is None
    assert not (user_dir() / "screen_test.toml").exists()
