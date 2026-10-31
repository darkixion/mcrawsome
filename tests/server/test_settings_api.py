import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcrawsome.server import app as app_module
from mcrawsome.server.app import create_app
from mcrawsome.server.config import ServerConfig
from mcrawsome.server.settings import InterfaceSettings, load_settings, settings_path

API = "/api/v1"


@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_studio_preference_migrates_without_losing_the_choice(
    client: TestClient, enabled: bool
) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"resolve_studio = {str(enabled).lower()}\n")
    assert load_settings()[0].resolve_full_resolution is enabled
    response = client.put(f"{API}/settings", json={"resolve_studio": enabled})
    assert response.status_code == 200
    assert response.json()["resolve_full_resolution"] is enabled
    assert "resolve_studio" not in tomllib.loads(path.read_text())


def test_settings_start_empty(client: TestClient, tmp_path: Path) -> None:
    assert client.get(f"{API}/settings").json() == {
        "folders": [],
        "workers": None,
        "show_preview_stats": False,
        "preview_gpu": True,
        "quad_bayer_remosaic": True,
        "preview_cache_mib": 256,
        "resolve_full_resolution": False,
        "resolve_separate_audio": False,
        "resolve_link_audio": True,
        "resolve_keywords": False,
        "timecode": "zero",
        "timecode_zone": "computer",
        "mount_folder": None,
        "keep_engine": False,
        "remount_on_start": False,
        "resolve_route": "auto",
        "problem": None,
        "mount_folder_default": str(tmp_path / "mounts"),
    }
    assert not settings_path().exists()


def test_a_missing_folder_is_refused_naming_the_field(client: TestClient, tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    response = client.put(f"{API}/settings", json={"folders": [str(missing)]})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "recipe-invalid"
    assert error["field"] == "folders"
    assert error["message"] == f"folders: {missing} does not exist or is not a folder"
    assert not settings_path().exists()


def test_other_invalid_values_name_their_field(client: TestClient) -> None:
    for body, field in (({"workers": 0}, "workers"), ({"resolve_route": "carrier pigeon"}, "resolve_route")):
        response = client.put(f"{API}/settings", json=body)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["field"] == field


def test_a_valid_put_is_saved_and_read_back(client: TestClient, tmp_path: Path) -> None:
    folder = tmp_path / "clips"
    folder.mkdir()
    settings = {
        "folders": [str(folder)],
        "workers": 3,
        "keep_engine": True,
        "resolve_route": "auto",
    }
    response = client.put(f"{API}/settings", json=settings)
    assert response.status_code == 200, response.text
    assert response.json() == settings | {
        "problem": None,
        "mount_folder_default": str(tmp_path / "mounts"),
        "show_preview_stats": False,
        "preview_gpu": True,
        "quad_bayer_remosaic": True,
        "preview_cache_mib": 256,
        "resolve_full_resolution": False,
        "resolve_separate_audio": False,
        "resolve_link_audio": True,
        "resolve_keywords": False,
        "timecode": "zero",
        "timecode_zone": "computer",
        "mount_folder": None,
        "remount_on_start": False,
    }
    assert client.get(f"{API}/settings").json() == settings | {
        "problem": None,
        "mount_folder_default": str(tmp_path / "mounts"),
        "show_preview_stats": False,
        "preview_gpu": True,
        "quad_bayer_remosaic": True,
        "preview_cache_mib": 256,
        "resolve_full_resolution": False,
        "resolve_separate_audio": False,
        "resolve_link_audio": True,
        "resolve_keywords": False,
        "timecode": "zero",
        "timecode_zone": "computer",
        "mount_folder": None,
        "remount_on_start": False,
    }
    path = settings_path()
    assert path == tmp_path / "config" / "mcrawsome" / "interface.toml"
    assert tomllib.loads(path.read_text())["folders"] == [str(folder)]
    assert [entry.name for entry in path.parent.iterdir()] == ["interface.toml"]  # no partial file left


def test_saved_settings_are_read_when_the_server_starts(library: Path, tmp_path: Path) -> None:
    folder = tmp_path / "clips"
    folder.mkdir()
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(f'folders = ["{folder}"]\nworkers = 2\n')
    app = create_app(ServerConfig(roots=[library]))
    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        saved = client.get(f"{API}/settings").json()
    assert saved["folders"] == [str(folder)]
    assert saved["workers"] == 2


def test_a_saved_folder_that_has_gone_does_not_stop_the_server(library: Path, tmp_path: Path) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(f'folders = ["{tmp_path / "unplugged"}"]\n')
    app = create_app(ServerConfig(roots=[library]))
    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        assert client.get(f"{API}/settings").json()["folders"] == [str(tmp_path / "unplugged")]


def _start(library: Path) -> TestClient:
    return TestClient(create_app(ServerConfig(roots=[library])), base_url="http://127.0.0.1:8642")


def test_a_broken_settings_file_starts_the_server_with_the_defaults_and_says_so(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "run_checks", list)
    folder = tmp_path / "clips"
    folder.mkdir()
    path = settings_path()
    path.parent.mkdir(parents=True)
    for text, says in (("folders = [\n", "Invalid value"), ("workers = 0\nkeep_engine = true\n", "workers")):
        path.write_text(text)
        with _start(library) as client:
            saved = client.get(f"{API}/settings").json()
            assert saved["keep_engine"] is False  # the defaults, nothing taken from the broken file
            assert str(path) in saved["problem"]
            assert says in saved["problem"]
            [check] = client.get(f"{API}/doctor").json()["checks"]
            assert check["status"] == "warn"
            assert check["detail"] == saved["problem"]
            assert path.read_text() == text  # left as it was until the user saves
            response = client.put(f"{API}/settings", json={"folders": [str(folder)]})
            assert response.status_code == 200, response.text
            assert response.json()["problem"] is None
            assert client.get(f"{API}/settings").json()["problem"] is None
            assert client.get(f"{API}/doctor").json()["checks"] == []
        assert tomllib.loads(path.read_text()) == {
            "folders": [str(folder)],
            "show_preview_stats": False,
            "preview_gpu": True,
            "quad_bayer_remosaic": True,
            "preview_cache_mib": 256,
            "resolve_full_resolution": False,
            "resolve_separate_audio": False,
            "resolve_link_audio": True,
            "resolve_keywords": False,
            "timecode": "zero",
            "timecode_zone": "computer",
            "keep_engine": False,
            "remount_on_start": False,
            "resolve_route": "auto",
        }


def test_a_key_from_a_later_version_is_ignored(library: Path) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text('keep_engine = true\ntheme = "light"\n')
    with _start(library) as client:
        saved = client.get(f"{API}/settings").json()
        assert saved["keep_engine"] is True
        assert saved["problem"] is None
        refused = client.put(f"{API}/settings", json={"theme": "light"})  # a request is still checked whole
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["field"] == "theme"


def test_the_problem_is_read_only(client: TestClient) -> None:
    response = client.put(f"{API}/settings", json={"problem": "none"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["field"] == "problem"


def test_a_folder_given_twice_is_saved_once(client: TestClient, tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    folders = [f"{first}/", str(second), str(first), str(tmp_path / "b" / ".." / "a")]
    response = client.put(f"{API}/settings", json={"folders": folders})
    assert response.status_code == 200, response.text
    assert response.json()["folders"] == [str(first), str(second)]
    assert tomllib.loads(settings_path().read_text())["folders"] == [str(first), str(second)]


def test_a_saved_folder_that_has_gone_does_not_block_other_changes(
    client: TestClient, tmp_path: Path
) -> None:
    unplugged, kept, other = tmp_path / "unplugged", tmp_path / "kept", tmp_path / "other"
    for folder in (unplugged, kept, other):
        folder.mkdir()
    folders = [str(unplugged), str(kept), str(other)]
    assert client.put(f"{API}/settings", json={"folders": folders}).status_code == 200
    unplugged.rmdir()

    response = client.put(f"{API}/settings", json={"folders": folders, "keep_engine": True})
    assert response.status_code == 200, response.text
    assert response.json()["keep_engine"] is True
    response = client.put(f"{API}/settings", json={"folders": folders[:2], "keep_engine": True})
    assert response.status_code == 200, response.text
    assert response.json()["folders"] == folders[:2]

    missing = tmp_path / "missing"
    response = client.put(f"{API}/settings", json={"folders": [*folders[:2], str(missing)]})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["field"] == "folders"
    assert error["message"] == f"folders: {missing} does not exist or is not a folder"
    assert client.get(f"{API}/settings").json()["folders"] == folders[:2]
    assert client.put(f"{API}/settings", json={"folders": [str(kept)]}).status_code == 200
    response = client.put(f"{API}/settings", json={"folders": [str(kept), str(unplugged)]})
    assert response.status_code == 422, response.text  # once removed, it is a new folder again


def test_interface_settings_expand_the_home_folder() -> None:
    settings = InterfaceSettings.model_validate({"folders": ["~/clips"], "destination": "~/exports"})
    assert settings.folders == [Path.home() / "clips"]
    assert settings.roots() == [Path.home() / "clips"]


def test_preview_gpu_setting_survives_reload(client: TestClient) -> None:
    response = client.put(f"{API}/settings", json={"preview_gpu": False})
    assert response.status_code == 200
    assert response.json()["preview_gpu"] is False
    assert client.get(f"{API}/settings").json()["preview_gpu"] is False
    assert tomllib.loads(settings_path().read_text())["preview_gpu"] is False


def test_preview_cache_budget_is_saved_and_validated(client: TestClient) -> None:
    response = client.put(f"{API}/settings", json={"preview_cache_mib": 64})
    assert response.status_code == 200
    assert client.get(f"{API}/settings").json()["preview_cache_mib"] == 64
    assert tomllib.loads(settings_path().read_text())["preview_cache_mib"] == 64
    for invalid in (-1, 2049):
        response = client.put(f"{API}/settings", json={"preview_cache_mib": invalid})
        assert response.status_code == 422
        assert response.json()["error"]["field"] == "preview_cache_mib"


def test_quad_bayer_remosaic_defaults_on_and_survives_reload(client: TestClient) -> None:
    assert client.get(f"{API}/settings").json()["quad_bayer_remosaic"] is True
    response = client.put(f"{API}/settings", json={"quad_bayer_remosaic": False})
    assert response.status_code == 200, response.text
    assert response.json()["quad_bayer_remosaic"] is False
    assert tomllib.loads(settings_path().read_text())["quad_bayer_remosaic"] is False
    from mcrawsome.server.settings import load_settings

    assert load_settings()[0].quad_bayer_remosaic is False


def test_preview_statistics_default_off_and_survive_reload(client: TestClient) -> None:
    assert client.get(f"{API}/settings").json()["show_preview_stats"] is False
    response = client.put(f"{API}/settings", json={"show_preview_stats": True})
    assert response.status_code == 200
    assert response.json()["show_preview_stats"] is True
    assert load_settings()[0].show_preview_stats is True


def test_bin_layout_preferences_persist_and_linking_defaults_on(client: TestClient) -> None:
    defaults = client.get(f"{API}/settings").json()
    assert defaults["resolve_separate_audio"] is False
    assert defaults["resolve_link_audio"] is True
    response = client.put(f"{API}/settings", json={"resolve_separate_audio": True})
    assert response.status_code == 200
    assert response.json()["resolve_link_audio"] is True
    response = client.put(
        f"{API}/settings", json={"resolve_separate_audio": True, "resolve_link_audio": False}
    )
    assert response.status_code == 200
    saved, problem = load_settings()
    assert problem is None
    assert saved.resolve_separate_audio is True
    assert saved.resolve_link_audio is False


def test_full_resolution_timelines_are_an_explicit_studio_preference(client: TestClient) -> None:
    assert client.get(f"{API}/settings").json()["resolve_full_resolution"] is False
    reply = client.put(f"{API}/settings", json={"resolve_full_resolution": True})
    assert reply.status_code == 200
    assert reply.json()["resolve_full_resolution"] is True
    assert load_settings()[0].resolve_full_resolution is True
