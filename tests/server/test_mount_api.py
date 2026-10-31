import json
import os
import struct
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from mcrawsome.io.clip import Clip
from mcrawsome.mount.manager import MountManager
from mcrawsome.resolve import native


@pytest.mark.parametrize("mode", ["bin", "timeline"])
@pytest.mark.parametrize("turn", [0, 90])
def test_reduced_send_links_full_original_and_retains_both_actual_dependencies(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    turn: int,
) -> None:
    from test_clips_api import listed_clip

    from mcrawsome.resolve.bin_codec import decode_map, decode_wire, unwrap

    if not MountManager().listing().available:
        pytest.skip("FUSE unavailable")
    identity, source = listed_clip(client, library, make_mcraw)
    source.with_suffix(".mcrawsome.toml").write_text(
        f'[develop]\nmount_proxy="quarter"\nrotation={turn}\nblack_level=42\n'
    )
    client.put("/api/v1/settings", json={"resolve_route": "menu"})
    selected = client.post("/api/v1/mounts", json={"clip_id": identity}).json()
    assert "id" in selected, selected
    try:
        monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("native send decoded RAW"))
        opened = []

        def open_archive(path: Path) -> None:
            records = client.get("/api/v1/mounts").json()["mounts"]
            dependencies = [m for m in records if m["retained"]]
            assert {m["raw_settings"]["mount_proxy"] for m in dependencies} == {"full", "quarter"}
            assert all(m["raw_settings"]["rotation"] == 0 for m in dependencies)
            opened.append(path)

        monkeypatch.setattr(native, "open_archive", open_archive)
        response = client.post("/api/v1/resolve/mounts/send", json={"mounts": [selected["id"]], "mode": mode})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["launched"], result
        assert result["results"][0]["id"] == "mount-" + selected["id"]
        assert len(opened) == 1
        with ZipFile(opened[0]) as archive:
            video = next(
                c
                for n in archive.namelist()
                if n.endswith("MpFolder.xml")
                for c in ET.fromstring(archive.read(n).decode().replace("ListMgt::", "ListMgt__")).iter(
                    "Sm2MpVideoClip"
                )
            )
            original = decode_map(bytes.fromhex(video.findtext("Video/BtVideoInfo/Geometry") or ""))
            assert original["Resolution"].value == struct.pack(">QQ", 128, 64)
            fields = decode_wire(unwrap(bytes.fromhex(video.findtext("FieldsBlob") or ""))[0])
            value = next(f.value for f in fields if f.number == 2)
            assert isinstance(value, bytes)
            proxy = decode_map(decode_map(value)["VIDEO_INFO"].value or b"")
            geometry = decode_map(proxy["Geometry"].value or b"")
            assert geometry["Resolution"].value == struct.pack(">QQ", 32, 16)

    finally:
        for record in client.get("/api/v1/mounts").json()["mounts"]:
            if record["source"] == str(source) and record["active"]:
                client.post(f"/api/v1/mounts/{record['id']}/unmount")


@pytest.mark.parametrize("turn", [90, 180, 270])
@pytest.mark.parametrize("mode", ["bin", "timeline"])
def test_native_sends_share_recorded_media_for_saved_turns_without_fuse(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    turn: int,
    mode: str,
) -> None:
    from mcrawsome.mount.manager import MountedSequence

    source = make_mcraw(library / "rotated.mcraw", [0, 41666667, 83333334])
    original = MountedSequence(
        id="a" * 24,
        source=source,
        path=library / "rotated",
        stamp=(1, 2, 3, 4),
        active=True,
        serving=True,
        frames=3,
        quad_bayer_remosaic=False,
    )
    original.raw_settings = original.raw_settings.model_copy(update={"rotation": turn, "black_level": 42})
    recorded = original.model_copy(
        update={
            "id": "b" * 24,
            "path": library / "recorded",
            "raw_settings": original.raw_settings.model_copy(update={"rotation": 0}),
        }
    )
    events: list[object] = []

    def mount(_self: object, path: Path, **kwargs: object) -> MountedSequence:
        assert path == source
        assert kwargs["expected"] == original.stamp
        assert kwargs["raw_settings"] == recorded.raw_settings
        assert kwargs["quad_bayer_remosaic"] is False
        events.append("prepare")
        return recorded

    def write(path: Path, mounts: list[MountedSequence], **kwargs: object) -> None:
        assert mounts == [recorded]
        assert kwargs["rotations"] == [turn]
        events.append("archive")
        path.write_bytes(b"archive")

    monkeypatch.setattr(MountManager, "get", lambda *_: original)
    monkeypatch.setattr(MountManager, "mount", mount)
    monkeypatch.setattr(MountManager, "retain", lambda _, ids: events.append(("retain", ids)))
    import mcrawsome.server.routes.mounts as routes

    monkeypatch.setattr(routes, "readable_media", lambda item: events.append(("readable", item.id)))
    monkeypatch.setattr(native, "write_bin", write)
    monkeypatch.setattr(native, "write_timeline", write)
    monkeypatch.setattr(native, "open_archive", lambda _: events.append("dispatch"))
    client.put("/api/v1/settings", json={"resolve_route": "menu"})
    result = client.post("/api/v1/resolve/mounts/send", json={"mounts": [original.id], "mode": mode})
    assert result.status_code == 200, result.text
    assert result.json()["launched"]
    assert result.json()["results"][0]["id"] == "mount-" + original.id
    assert events == ["prepare", "archive", ("readable", recorded.id), ("retain", [recorded.id]), "dispatch"]
    assert original.raw_settings.rotation == turn


def test_a_send_tells_the_page_each_clips_stage_by_the_mount_the_page_sent(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.mount.manager import MountedSequence
    from mcrawsome.server.progress import ProgressHub

    source = make_mcraw(library / "rotated.mcraw", [0, 41666667, 83333334])
    original = MountedSequence(
        id="a" * 24,
        source=source,
        path=library / "rotated",
        stamp=(1, 2, 3, 4),
        active=True,
        serving=True,
        frames=3,
        quad_bayer_remosaic=False,
    )
    original.raw_settings = original.raw_settings.model_copy(update={"rotation": 90})
    recorded = original.model_copy(  # the timeline is served from the recorded-orientation media
        update={
            "id": "b" * 24,
            "path": library / "recorded",
            "raw_settings": original.raw_settings.model_copy(update={"rotation": 0}),
        }
    )

    def write(path: Path, mounts: list[MountedSequence], **kwargs: Any) -> None:
        assert mounts == [recorded]
        kwargs["progress"](mounts[0], "Reading its gyro")
        kwargs["progress"](None, "Writing the timeline")
        path.write_bytes(b"archive")

    published: list[dict[str, object]] = []
    monkeypatch.setattr(ProgressHub, "publish", lambda _hub, stage: published.append(stage))
    monkeypatch.setattr(MountManager, "get", lambda *_: original)
    monkeypatch.setattr(MountManager, "mount", lambda *_a, **_kw: recorded)
    monkeypatch.setattr(MountManager, "retain", lambda *_: None)
    import mcrawsome.server.routes.mounts as routes

    monkeypatch.setattr(routes, "readable_media", lambda _item: None)
    monkeypatch.setattr(native, "write_timeline", write)
    monkeypatch.setattr(native, "open_archive", lambda _: None)
    client.put("/api/v1/settings", json={"resolve_route": "menu"})
    sent = {"mounts": [original.id], "mode": "timeline", "progress": "page-1"}
    response = client.post("/api/v1/resolve/mounts/send", json=sent)
    assert response.status_code == 200, response.text
    assert published == [
        {"send": "page-1", "mount": "mount-" + original.id, "stage": "Reading its gyro"},
        {"send": "page-1", "mount": None, "stage": "Writing the timeline"},
        {"send": "page-1", "mount": None, "stage": "Opening it in Resolve"},
    ]
    published.clear()
    response = client.post("/api/v1/resolve/mounts/send", json={"mounts": [original.id], "mode": "timeline"})
    assert response.status_code == 200, response.text
    assert published == []  # an older page asks for no stages


@pytest.mark.parametrize("token", ["", "has space", "x" * 65, "../events"])
def test_a_send_refuses_a_progress_token_it_would_not_echo(client: TestClient, token: str) -> None:
    sent = {"mounts": ["unknown"], "progress": token}
    assert client.post("/api/v1/resolve/mounts/send", json=sent).status_code == 422
    sent["progress"] = "lzx1c2-4fzyo82w"
    assert client.post("/api/v1/resolve/mounts/send", json=sent).status_code != 422


@pytest.mark.parametrize(
    "payload",
    [{"mode": "bin", "gyro": True}, {"mode": "timeline", "gyro": False, "rolling_shutter": True}],
)
def test_older_pages_gyro_flags_are_accepted_and_ignored(
    client: TestClient, payload: dict[str, object]
) -> None:
    """A timeline stabilises each clip by its own Stabilise settings; the flags no longer matter."""
    response = client.post("/api/v1/resolve/mounts/send", json={"mounts": ["unknown"], **payload})
    assert response.status_code != 422


def test_an_older_pages_unchecked_flag_is_accepted_and_changes_nothing(client: TestClient) -> None:
    """Every gyro timeline now sends; the flag an older page adds is no longer a request error."""
    response = client.post(
        "/api/v1/resolve/mounts/send",
        json={"mounts": ["unknown"], "mode": "timeline", "gyro": False, "unchecked_gyro": True},
    )
    assert response.status_code != 422


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_timeline_uses_recorded_raw_mount_and_clip_rotation(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    rotation: int,
) -> None:
    from test_clips_api import listed_clip

    from mcrawsome.output.dng import read_dng_tags
    from mcrawsome.resolve.bin_codec import decode_wire, unwrap

    if not MountManager().listing().available:
        pytest.skip("FUSE unavailable")
    identity, source = listed_clip(client, library, make_mcraw)
    source.with_suffix(".mcrawsome.toml").write_text(
        f"[develop]\nrotation={rotation}\nblack_level=42\nlens_shading=0\n"
    )
    client.put("/api/v1/settings", json={"resolve_route": "menu"})
    selected = client.post("/api/v1/mounts", json={"clip_id": identity}).json()
    opened: list[Path] = []
    monkeypatch.setattr(native, "open_archive", opened.append)
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("send decoded RAW"))
    try:
        original = Path(selected["path"]) / "a-000000.dng"
        original_bytes = original.read_bytes()
        result = client.post(
            "/api/v1/resolve/mounts/send", json={"mounts": [selected["id"]], "mode": "timeline"}
        ).json()
        assert result["launched"], result
        assert result["results"][0]["id"] == "mount-" + selected["id"]
        assert original.read_bytes() == original_bytes
        mounts = client.get("/api/v1/mounts").json()["mounts"]
        recorded = next(m for m in mounts if m["raw_settings"]["rotation"] == 0)
        assert recorded["retained"]
        assert recorded["sent"]
        assert recorded["raw_settings"]["black_level"] == 42
        assert recorded["raw_settings"]["lens_shading"] == 0
        raw_dng = Path(recorded["path"]) / "a-000000.dng"
        tags = read_dng_tags(raw_dng)
        assert tags[274] == (1,)
        assert tags[50714] == (42,) * 4
        assert raw_dng.read_bytes()[-128 * 64 * 2 :] == original_bytes[-128 * 64 * 2 :]
        with ZipFile(result["archive"]) as archive:
            entry = next(n for n in archive.namelist() if n.startswith("SeqContainer/"))
            container = ET.fromstring(archive.read(entry))
            video = container.find(".//Sm2TiVideoClip")
            assert video is not None
            assert not video.findtext("EffectFiltersBA")
            master = ET.fromstring(
                archive.read("MediaPool/Master/MpFolder.xml").decode().replace("ListMgt::", "ListMgt__")
            )
            radiometry = master.findtext(".//BtVideoInfo/Radiometry")
            assert radiometry
            fields = decode_wire(unwrap(bytes.fromhex(radiometry))[0])
            assert next(f.value for f in fields if f.number == 19) == 2 * rotation
            assert str(recorded["path"]) in (video.findtext("MediaFilePath") or "")
            assert not container.findtext(".//Sm2TiAudioClip/EffectFiltersBA")
        # The retired enable switch must not leave native sending disabled.
        client.put("/api/v1/settings", json={"resolve_route": "off"})
        resent = client.post(
            "/api/v1/resolve/mounts/send", json={"mounts": [selected["id"]], "mode": "timeline"}
        ).json()
        assert resent["archive"] is not None
        assert len(opened) == 2
        assert len(client.get("/api/v1/mounts").json()["mounts"]) == 2
    finally:
        for mount in client.get("/api/v1/mounts").json()["mounts"]:
            client.post(f"/api/v1/mounts/{mount['id']}/unmount")


def test_actual_media_is_rechecked_after_timeline_creation_before_dispatch(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_clips_api import listed_clip

    import mcrawsome.server.routes.mounts as route

    if not MountManager().listing().available:
        pytest.skip("FUSE unavailable")
    identity, _ = listed_clip(client, library, make_mcraw)
    client.put("/api/v1/settings", json={"resolve_route": "menu"})
    mount = client.post("/api/v1/mounts", json={"clip_id": identity}).json()
    opened: list[Path] = []
    monkeypatch.setattr(native, "open_archive", opened.append)

    def unreadable(mount: object) -> int:
        raise ValueError("Virtual media is not readable; use Remount")

    monkeypatch.setattr(route, "readable_media", unreadable)
    try:
        result = client.post(
            "/api/v1/resolve/mounts/send", json={"mounts": [mount["id"]], "mode": "timeline"}
        ).json()
        assert not result["launched"]
        assert "Remount" in result["results"][0]["message"]
        assert opened == []
    finally:
        client.post(f"/api/v1/mounts/{mount['id']}/unmount")


def test_virtual_handoff_has_no_export_job_and_preserves_engine(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = MountManager().listing()
    if not status.available:
        pytest.skip(status.reason)
    path = make_mcraw(library / "virtual.mcraw", [0, 41_666_667, 83_333_334])
    os.utime(path, (time.time() - 60,) * 2)
    client.put("/api/v1/settings", json={"folders": [str(library)], "resolve_route": "menu"})
    [clip] = client.get("/api/v1/library").json()["clips"]
    response = client.post("/api/v1/mounts", json={"clip_id": clip["id"]})
    assert response.status_code == 201, response.text
    mounted = response.json()
    identity = mounted["id"]
    opened: list[Path] = []
    monkeypatch.setattr(native, "open_archive", opened.append)
    try:
        assert mounted["active"]
        assert client.get("/api/v1/health").json()["active_mounts"] == 1
        with closing(
            TestClient(client.app, base_url="http://127.0.0.1:8642", client=("127.0.0.1", 1234))
        ) as local:
            stopped = local.post("/api/v1/engine/stop")
        assert stopped.status_code == 409
        assert "mount" in stopped.json()["error"]["message"]
        sent = client.post("/api/v1/resolve/mounts/send", json={"mounts": [identity]})
        assert sent.status_code == 200, sent.text
        result = sent.json()
        assert result["launched"]
        archive = Path(result["archive"])
        assert archive.suffix == ".drb"
        assert archive.is_file()
        assert opened == [archive]
        retained = client.get("/api/v1/mounts").json()["mounts"][0]
        assert retained["sent"]
        assert retained["retained"]
        assert not result["results"][0]["added"]  # opening does not prove an import
        assert client.get("/api/v1/send/pending?target=resolve").status_code == 404

        monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("native sends must not decode pixels"))
        paired = client.post("/api/v1/resolve/mounts/send", json={"mounts": [identity], "mode": "timeline"})
        assert paired.status_code == 200, paired.text
        timeline = Path(paired.json()["archive"])
        assert timeline.suffix == ".drt"
        assert paired.json()["launched"]
        assert opened == [archive, timeline]
        with ZipFile(timeline) as native_timeline:
            assert any(name.startswith("SeqContainer/") for name in native_timeline.namelist())

        # A desktop launch failure keeps both the archive and the mount for recovery.
        def refused(_path: Path) -> None:
            raise OSError("No application is associated with DRB")

        monkeypatch.setattr(native, "open_archive", refused)
        failed = client.post("/api/v1/resolve/mounts/send", json={"mounts": [identity]}).json()
        assert not failed["launched"]
        assert Path(failed["archive"]).is_file()
        assert "No application" in failed["results"][0]["message"]
        assert client.get("/api/v1/mounts").json()["mounts"][0]["active"]
        assert client.put("/api/v1/settings", json={"resolve_route": "off"}).status_code == 200
        resent = client.post("/api/v1/resolve/mounts/send", json={"mounts": [identity]}).json()
        assert resent["archive"] is not None
        assert not resent["launched"]
    finally:
        assert client.post(f"/api/v1/mounts/{identity}/unmount").status_code == 200
    assert client.get("/api/v1/health").json()["active_mounts"] == 0
    assert client.get("/api/v1/mounts").json()["mounts"] == []
    assert client.post(f"/api/v1/mounts/{identity}/remount").status_code == 404
    assert client.post("/api/v1/resolve/mounts/send", json={"mounts": [identity]}).status_code in (404, 422)


def test_retired_restore_actions_are_unavailable(client: TestClient) -> None:
    assert client.post("/api/v1/mounts/restore-sent").status_code in (404, 405)
    assert client.post("/api/v1/mounts/restore-sent/cancel").status_code in (404, 405)
    saved = client.put("/api/v1/settings", json={"restore_sent_media": True})
    assert saved.status_code == 200
    assert "restore_sent_media" not in saved.json()
    assert "restoration" not in client.get("/api/v1/mounts").json()


def test_mount_api_refuses_arbitrary_paths_and_unknown_ids(client: TestClient) -> None:
    assert client.post("/api/v1/mounts", json={"clip": "/etc/passwd"}).status_code == 422
    assert client.post("/api/v1/mounts", json={"clip_id": "unknown"}).status_code == 404
    assert client.post("/api/v1/mounts/no/unmount").status_code == 404
    assert client.post("/api/v1/resolve/mounts/send", json={"mounts": []}).status_code == 422
    assert (
        client.post(
            "/api/v1/resolve/mounts/send", json={"mounts": ["unknown"], "mode": "invented"}
        ).status_code
        == 422
    )


def test_mount_snapshots_resolved_raw_calibration_and_keeps_old_generation(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    from test_clips_api import listed_clip

    from mcrawsome.output.dng import read_dng_tags

    if not MountManager().listing().available:
        pytest.skip("FUSE unavailable")
    identity, _ = listed_clip(client, library, make_mcraw)
    (library / "mcrawsome.toml").write_text("[develop]\nblack_level = 42\nwhite_level = 3000\n")
    url = f"/api/v1/clips/{identity}/recipe"
    view = client.get(url).json()
    assert view["inherited"]["develop"]["black_level"] == 42
    before = client.post("/api/v1/mounts", json={"clip_id": identity}).json()
    mounted = [before["id"]]
    try:
        response = client.put(
            url,
            json={
                "version": view["layer"]["version"],
                "values": {
                    "develop": {"black_level": [10, 20, 30, 40], "lens_shading": 0},
                    "look": "bond",
                },
            },
        )
        assert response.status_code == 200, response.text
        after = client.post("/api/v1/mounts", json={"clip_id": identity}).json()
        mounted.append(after["id"])
        assert after["id"] != before["id"]
        assert after["raw_settings"]["black_level"] == [10, 20, 30, 40]
        assert after["raw_settings"]["white_level"] == 3000
        assert read_dng_tags(Path(before["path"]) / "a-000000.dng")[50714] == (42,) * 4
        assert read_dng_tags(Path(after["path"]) / "a-000000.dng")[50714] == (10, 20, 30, 40)
        # Removing this clip's override reveals the folder's calibration, preserving other edits.
        cleared = client.put(url, json={"version": response.json()["version"], "values": {"look": "bond"}})
        assert cleared.status_code == 200, cleared.text
        assert client.get(url).json()["resolved"]["develop"]["black_level"] == 42
        assert client.post("/api/v1/mounts", json={"clip_id": identity}).json()["id"] == before["id"]
    finally:
        for mount_id in mounted:
            client.post(f"/api/v1/mounts/{mount_id}/unmount")


def test_media_use_events_follow_handle_changes_without_reloading_mounts(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from fastapi import Request
    from starlette.types import Message

    import mcrawsome.server.app as module
    from mcrawsome.library.scan import Library
    from mcrawsome.library.watch import LibraryWatcher
    from mcrawsome.mount.manager import process_start

    manager = MountManager()
    folder = manager.root / ("a" * 24)
    folder.mkdir(parents=True)
    process = {"pid": os.getpid(), "start": process_start(os.getpid())}
    (folder / "ready.json").write_text(json.dumps(process))
    (folder / "usage.json").write_text(json.dumps(process | {"in_use": False}))
    monkeypatch.setattr(module, "EVENT_POLL_S", 0.001)
    monkeypatch.setattr(manager, "listing", lambda: pytest.fail("usage events must not scan media records"))
    watcher = LibraryWatcher(Library(), lambda: [library])

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def follow() -> None:
        stream = module._events(
            watcher, Request({"type": "http"}, receive), threading.Event(), mounts=manager
        )
        try:
            await anext(stream)
            event = await asyncio.wait_for(anext(stream), 2)
            assert event.startswith("event: media-use\n")
            assert json.loads(event.split("data: ")[1]) == {"a" * 24: False}
            (folder / "usage.json").write_text(json.dumps(process | {"in_use": True}))
            event = await asyncio.wait_for(anext(stream), 2)
            assert json.loads(event.split("data: ")[1]) == {"a" * 24: True}
        finally:
            await stream.aclose()

    asyncio.run(follow())


@pytest.mark.parametrize(
    ("trim", "expected"),
    [
        ({}, None),
        ({"start": "1f"}, (1, 2)),
        ({"start": "1f", "duration": "1f"}, (1, 1)),
        ({"start": "0s", "duration": "3f"}, None),  # the whole recording: the same media as unmarked
    ],
)
def test_the_in_and_out_marks_choose_the_mounted_frames(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    trim: dict[str, str],
    expected: tuple[int, int] | None,
) -> None:
    from test_clips_api import listed_clip

    from mcrawsome.mount.manager import MountedSequence

    identity, _ = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/recipe"
    version = client.get(url).json()["layer"]["version"]
    if trim:
        assert client.put(url, json={"version": version, "values": {"trim": trim}}).status_code == 200
    asked: list[object] = []

    def mount(_self: object, path: Path, **kwargs: object) -> MountedSequence:
        asked.append(kwargs["frame_range"])
        return MountedSequence(id="a" * 24, source=path, path=library / "m", stamp=(1, 2, 3, 4))

    monkeypatch.setattr(MountManager, "mount", mount)
    response = client.post("/api/v1/mounts", json={"clip_id": identity})
    assert response.status_code == 201, response.text
    assert asked == [expected]


@pytest.mark.parametrize(("separate", "linked"), [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize("full_resolution", [False, True])
def test_send_uses_current_global_resolve_preferences(
    client: TestClient,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    separate: bool,
    linked: bool,
    full_resolution: bool,
) -> None:
    from mcrawsome.mount.manager import MountedSequence
    from mcrawsome.resolve import native

    identity = "a" * 24
    mount = MountedSequence(
        id=identity,
        source=library / "clip.mcraw",
        path=library / "media",
        stamp=(1, 2, 3, 4),
        active=True,
        serving=True,
    )
    monkeypatch.setattr(MountManager, "get", lambda self, selected: mount)
    seen: list[tuple[bool, bool, bool]] = []

    def send(mounts: object, route: object, **kwargs: object) -> native.NativeSendResult:
        seen.append(
            (bool(kwargs["separate_audio"]), bool(kwargs["link_audio"]), bool(kwargs["full_resolution"]))
        )
        return native.NativeSendResult(results=[native.OutputResult(id=identity, message="Prepared")])

    monkeypatch.setattr(native, "send_mounts", send)
    response = client.put(
        "/api/v1/settings",
        json={
            "resolve_separate_audio": separate,
            "resolve_link_audio": linked,
            "resolve_full_resolution": full_resolution,
        },
    )
    assert response.status_code == 200, response.text
    response = client.post("/api/v1/resolve/mounts/send", json={"mounts": [identity]})
    assert response.status_code == 200, response.text
    assert seen == [(separate, linked, full_resolution)]


def test_a_timeline_send_learns_each_clips_rate_before_anything_is_mounted(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page asks first, so a clip a timeline cannot take is named before four others are mounted."""
    for name, step in {"a": 41_666_667, "b": 42_030_934, "c": 8_333_333}.items():  # 24, 23.79 and 120 fps
        make_mcraw(library / f"{name}.mcraw", [i * step for i in range(12)])
        os.utime(library / f"{name}.mcraw", (time.time() - 60,) * 2)
    assert client.put("/api/v1/settings", json={"folders": [str(library)]}).status_code == 200
    ids = {clip["name"]: clip["id"] for clip in client.get("/api/v1/library").json()["clips"]}
    monkeypatch.setattr(MountManager, "mount", lambda *args, **kwargs: pytest.fail("nothing is mounted"))
    chosen = [ids["a.mcraw"], ids["b.mcraw"], ids["c.mcraw"]]
    response = client.post("/api/v1/resolve/timeline/rates", json={"clip_ids": chosen})
    assert response.status_code == 200, response.text
    rates = response.json()
    assert [(clip["name"], clip["rate"], clip["usable"], clip["nearest"]) for clip in rates["clips"]] == [
        ("a.mcraw", 24.0, True, 24),
        ("b.mcraw", 23.792, False, 24),
        ("c.mcraw", 120.0, False, None),  # Nearest whole rate cannot help: timelines take up to 60
    ]
    assert [clip["id"] for clip in rates["clips"]] == chosen
    assert "b (23.792 fps) and c (120 fps)" in rates["message"]

    url = f"/api/v1/clips/{ids['b.mcraw']}/recipe"
    version = client.get(url).json()["layer"]["version"]
    values = {"develop": {"mount_frame_rate": "integer"}}
    assert client.put(url, json={"version": version, "values": values}).status_code == 200
    rates = client.post("/api/v1/resolve/timeline/rates", json={"clip_ids": chosen[:2]}).json()
    assert [(clip["rate"], clip["usable"]) for clip in rates["clips"]] == [(24.0, True), (24.0, True)]
    assert rates["message"] is None


def test_the_rate_check_refuses_clips_a_timeline_cannot_mount(client: TestClient) -> None:
    response = client.post("/api/v1/resolve/timeline/rates", json={"clip_ids": ["0" * 24]})
    assert response.status_code == 404
    assert client.post("/api/v1/resolve/timeline/rates", json={"clip_ids": []}).status_code == 422
