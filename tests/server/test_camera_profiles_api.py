import os
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome.camera.calibrate_lens import LensAnalysis
from mcrawsome.camera.profile import CameraProfile, Match, all_profiles, user_dir


def test_target_download_is_a_printable_svg(client: TestClient) -> None:
    response = client.get("/api/v1/camera-profiles/target.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "attachment" in response.headers["content-disposition"]
    assert "Move the phone" in response.text


def test_profile_persistence_is_explicit_and_local_only(client: TestClient) -> None:
    all_profiles.cache_clear()
    profile = CameraProfile(
        name="test_phone",
        match=Match(device_model="Test phone", camera_id="0/2", width=640, height=480, fps=24),
    )
    try:
        with closing(
            TestClient(client.app, base_url="http://127.0.0.1:8642", client=("192.0.2.1", 1234))
        ) as remote:
            assert (
                remote.post(
                    "/api/v1/camera-profiles", json={"profile": profile.model_dump(mode="json")}
                ).status_code
                == 403
            )
        with closing(
            TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 1234))
        ) as local:
            response = local.post(
                "/api/v1/camera-profiles", json={"profile": profile.model_dump(mode="json")}
            )
            assert response.status_code == 201
            assert (user_dir() / "test_phone.toml").is_file()
            assert (
                local.post(
                    "/api/v1/camera-profiles", json={"profile": profile.model_dump(mode="json")}
                ).status_code
                == 409
            )
            listed = local.get("/api/v1/camera-profiles").json()
            assert any(
                item["profile"]["name"] == "test_phone" and item["user"] for item in listed["profiles"]
            )
            download = local.get("/api/v1/camera-profiles/test_phone.toml")
            assert download.status_code == 200
            assert 'camera_id = "0/2"' in download.text
    finally:
        all_profiles.cache_clear()


def test_analysis_does_not_save_until_user_accepts(
    client: TestClient, tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.server.routes import camera_profiles as routes

    profile = CameraProfile(
        name="analysed_phone",
        match=Match(device_model="Test phone", camera_id="0/2", width=640, height=480, fps=24),
    )
    monkeypatch.setattr(
        routes,
        "analyse_lens",
        lambda *args: LensAnalysis(profile=profile, sampled_images=60, detected_images=24, notes=[]),
    )
    os.utime(tiny_raw, (time.time() - 60,) * 2)
    with closing(
        TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 1234))
    ) as local:
        [clip] = local.post("/api/v1/opened-clips", json={"paths": [str(tiny_raw)]}).json()["clips"]
        result = local.post(
            "/api/v1/camera-profiles/analyse", json={"clip_id": clip["id"], "model": "opencv_standard"}
        )
        assert result.status_code == 200, result.text
        assert result.json()["profile"]["match"]["camera_id"] == "0/2"
        assert not (user_dir() / "analysed_phone.toml").exists()


def test_direct_recording_analysis_keeps_the_open_session_unchanged(
    client: TestClient, tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.server.routes import camera_profiles as routes

    profile = CameraProfile(name="direct_camera")
    calls: list[Path] = []

    def analyse(path: Path, model: str, stop: object) -> LensAnalysis:
        calls.append(path)
        return LensAnalysis(profile=profile, sampled_images=60, detected_images=24, notes=[])

    monkeypatch.setattr(routes, "analyse_lens", analyse)
    with closing(
        TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 1234))
    ) as local:
        before = local.get("/api/v1/session").json()
        response = local.post("/api/v1/camera-profiles/analyse", json={"path": str(tiny_raw)})
        assert response.status_code == 200
        assert calls == [tiny_raw]
        assert local.get("/api/v1/session").json() == before
        assert local.post("/api/v1/camera-profiles/analyse", json={}).status_code == 422
        assert (
            local.post(
                "/api/v1/camera-profiles/analyse", json={"path": str(tiny_raw), "clip_id": "a"}
            ).status_code
            == 422
        )
    with closing(
        TestClient(client.app, base_url="http://127.0.0.1:8642", client=("192.0.2.1", 1234))
    ) as remote:
        assert remote.post("/api/v1/camera-profiles/analyse", json={"path": str(tiny_raw)}).status_code == 403
