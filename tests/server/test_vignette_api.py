from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_clips_api import listed_clip


def test_vignette_profile_edits_are_versioned_and_keep_other_settings(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, path = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}"
    profile = client.get(url + "/vignette-profile")
    assert profile.status_code == 200, profile.text
    recipe = client.get(url + "/recipe").json()
    saved = client.put(
        url + "/recipe",
        json={
            "version": recipe["layer"]["version"],
            "values": {"look": "bond", "trim": {"start": "1f"}, "develop": {"exposure": 2}},
        },
    ).json()
    update = {"version": saved["version"], "profile": profile.json()}
    applied = client.put(url + "/vignette-profile", json=update)
    assert applied.status_code == 200, applied.text
    assert applied.json()["values"]["develop"]["exposure"] == 2
    assert applied.json()["values"]["trim"] == {"start": "1f"}
    assert client.put(url + "/vignette-profile", json=update).status_code == 409
    wrong = profile.json() | {"sensor_width": 1024}
    assert (
        client.put(
            url + "/vignette-profile", json={"version": applied.json()["version"], "profile": wrong}
        ).status_code
        == 422
    )
    cleared = client.put(
        url + "/vignette-profile", json={"version": applied.json()["version"], "profile": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["values"] == {"look": "bond", "trim": {"start": "1f"}, "develop": {"exposure": 2}}
    assert path.is_file()


def test_generic_recipe_cannot_bypass_profile_compatibility(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, _ = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}"
    profile = client.get(url + "/vignette-profile").json()
    profile["cfa"] = "bggr" if profile["cfa"] != "bggr" else "rggb"
    result = client.put(
        url + "/recipe",
        json={"version": "", "values": {"develop": {"vignette_profile": profile}}},
    )
    assert result.status_code == 422, result.text
    assert client.get(url + "/recipe").json()["layer"]["values"] == {}


def test_live_mount_keeps_profile_snapshot_and_refuses_edits(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    from mcrawsome.mount.manager import MountManager

    if not MountManager().listing().available:
        pytest.skip("FUSE unavailable")
    identity, _ = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}"
    profile = client.get(url + "/vignette-profile").json()
    profile["gains"] = [[2.0] * (profile["map_width"] * profile["map_height"])] * 4
    applied = client.put(url + "/vignette-profile", json={"version": "", "profile": profile}).json()
    result = client.post("/api/v1/mounts", json={"clip_id": identity})
    assert result.status_code == 201, result.text
    mount = result.json()
    unmount = f"/api/v1/mounts/{mount['id']}/unmount"
    try:
        assert (
            client.put(
                url + "/vignette-profile", json={"version": applied["version"], "profile": None}
            ).status_code
            == 409
        )
        refused = client.put(url + "/recipe", json={"version": applied["version"], "values": {}})
        assert refused.status_code == 409, refused.text
        # Other Develop edits leave the mounted profile alone.
        allowed = client.put(
            url + "/recipe",
            json={
                "version": applied["version"],
                "values": {"develop": {"vignette_profile": profile, "exposure": 1}},
            },
        )
        assert allowed.status_code == 200, allowed.text
    finally:
        client.post(unmount)
    version = client.get(url + "/recipe").json()["layer"]["version"]
    assert (
        client.put(url + "/vignette-profile", json={"version": version, "profile": None}).status_code == 200
    )
    # Unmounting removed the record, so its snapshot cannot come back.
    assert client.post(f"/api/v1/mounts/{mount['id']}/remount").status_code == 404
