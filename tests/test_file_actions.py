"""Headless sends and mounts reuse saved mount settings and never create/replace a window; headless unmounts
take only the chosen recordings' media, the normal way."""

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from mcrawsome import file_actions as module
from mcrawsome.cli.main import cli
from mcrawsome.errors import McError


@pytest.mark.parametrize("mode", ["bin", "timeline"])
@pytest.mark.parametrize("running", [True, False])
def test_send_reuses_engine_and_batches_prepared_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, running: bool
) -> None:
    files = [tmp_path / 'one "quote" %.mcraw', tmp_path / "two.MCRAW"]
    for path in files:
        path.touch()
    started: list[int] = []
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(module, "answers", lambda port: running)
    monkeypatch.setattr(module, "start_server", lambda port, roots: started.append(port))

    def request(port: int, path: str, method: str = "GET", body: Any = None, **kwargs: Any) -> dict[str, Any]:
        assert port == 18911
        calls.append((path, body))
        if path == "/mounts" and method == "GET":
            return {"available": True, "mounts": [{"id": "owner-mount"}]}
        if path == "/opened-clips":
            assert body == {"paths": list(map(str, files))}
            return {
                "clips": [
                    {"id": str(i), "path": str(p), "kind": "raw", "status": "ready"}
                    for i, p in enumerate(files)
                ]
            }
        if path == "/resolve/timeline/rates":
            assert body == {"clip_ids": ["0", "1"]}
            return {"clips": [], "message": None}
        if path == "/mounts":
            assert set(body) == {"clip_id"}  # the API resolves the saved settings; no defaults overwrite them
            return {"id": "mount-" + body["clip_id"]}
        assert path == "/resolve/mounts/send"
        assert body == {"mounts": ["mount-0", "mount-1"], "mode": mode}
        return {"launched": True, "archive": "handoff", "results": []}

    monkeypatch.setattr(module, "request", request)
    result = CliRunner().invoke(
        cli, ["send-to-resolve", "--port", "18911", "--mode", mode, "--", *map(str, files), str(files[0])]
    )
    assert result.exit_code == 0, result.output
    assert "Complete the import in Resolve" in result.output
    assert started == ([] if running else [18911])
    assert [path for path, _ in calls] == [
        "/mounts",
        "/opened-clips",
        *(["/resolve/timeline/rates"] if mode == "timeline" else []),
        "/mounts",
        "/mounts",
        "/resolve/mounts/send",
    ]


def test_a_timeline_send_names_clips_at_rates_it_cannot_take_before_mounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "odd.mcraw"
    clip.touch()
    monkeypatch.setattr(module, "answers", lambda port: True)
    says = "odd records 23.792 fps, and Resolve timelines need a whole number of frames per second"

    def request(port: int, path: str, method: str = "GET", body: Any = None, **kwargs: Any) -> dict[str, Any]:
        if path == "/mounts" and method == "GET":
            return {"available": True}
        if path == "/opened-clips":
            return {"clips": [{"id": "7", "path": str(clip), "kind": "raw", "status": "ready"}]}
        assert path == "/resolve/timeline/rates", f"{path} was called after the rates were refused"
        return {"clips": [], "message": says}

    monkeypatch.setattr(module, "request", request)
    with pytest.raises(McError, match=r"odd records 23\.792 fps"):
        module.send_files(18911, (clip,), "timeline", lambda message: None)


def test_unavailable_mounting_or_unready_recordings_do_not_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mcraw"
    clip.touch()
    monkeypatch.setattr(module, "answers", lambda port: True)
    monkeypatch.setattr(
        module, "request", lambda *args, **kwargs: {"available": False, "reason": "FUSE unavailable"}
    )
    with pytest.raises(McError, match="FUSE unavailable"):
        module.send_files(18911, (clip,), "bin", lambda message: None)

    def request(port: int, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if path == "/mounts":
            return {"available": True}
        assert path == "/opened-clips"
        return {"clips": [{"path": str(clip), "kind": "raw", "id": None, "status": "copying"}]}

    monkeypatch.setattr(module, "request", request)
    with pytest.raises(McError, match="not ready"):
        module.send_files(18911, (clip,), "bin", lambda message: None)


def test_headless_errors_preserve_the_engine_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise urllib.error.HTTPError(
            "local",
            400,
            "error",
            Message(),
            io.BytesIO(
                json.dumps({"error": {"code": "disk-full", "message": "No space for the archive"}}).encode()
            ),
        )

    monkeypatch.setattr(module, "request", request)
    with pytest.raises(McError, match="No space for the archive") as caught:
        module._call(18911, "/resolve/mounts/send")
    assert caught.value.code == "disk-full"


def test_identical_copies_use_library_aliases_and_send_one_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [tmp_path / "first.mcraw", tmp_path / "second.mcraw"]
    for file in files:
        file.touch()
    calls: list[str] = []
    monkeypatch.setattr(module, "answers", lambda port: True)

    def request(port: int, path: str, method: str = "GET", body: Any = None, **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        if path == "/mounts" and method == "GET":
            return {"available": True}
        if path == "/opened-clips":
            return {
                "clips": [
                    {
                        "path": str(files[0]),
                        "paths": list(map(str, files)),
                        "id": "same-recording",
                        "kind": "raw",
                        "status": "ready",
                    }
                ]
            }
        if path == "/mounts":
            assert body == {"clip_id": "same-recording"}
            return {"id": "one-mount"}
        assert body == {"mounts": ["one-mount"], "mode": "bin"}
        return {"launched": True}

    monkeypatch.setattr(module, "request", request)
    module.send_files(18911, tuple(files), "bin", lambda message: None)
    assert calls.count("/mounts") == 2  # one availability request and one mount


@pytest.mark.parametrize("running", [True, False])
def test_mount_prepares_media_without_sending_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    files = [tmp_path / 'one "quote" %.mcraw', tmp_path / "two.MCRAW"]
    for path in files:
        path.touch()
    started: list[int] = []
    calls: list[str] = []
    monkeypatch.setattr(module, "answers", lambda port: running)
    monkeypatch.setattr(module, "start_server", lambda port, roots: started.append(port))

    def request(port: int, path: str, method: str = "GET", body: Any = None, **kwargs: Any) -> dict[str, Any]:
        assert port == 18911
        calls.append(path)
        if path == "/mounts" and method == "GET":
            return {"available": True, "mounts": []}
        if path == "/opened-clips":
            assert body == {"paths": list(map(str, files))}
            return {
                "clips": [
                    {"id": str(i), "path": str(p), "kind": "raw", "status": "ready"}
                    for i, p in enumerate(files)
                ]
            }
        assert path == "/mounts", f"{path} was called"
        assert set(body) == {"clip_id"}  # the API resolves the saved settings; no defaults overwrite them
        return {
            "id": "mount-" + body["clip_id"],
            "path": str(tmp_path / "Mounts" / f"clip-{body['clip_id']}"),
        }

    monkeypatch.setattr(module, "request", request)
    result = CliRunner().invoke(cli, ["mount", "--port", "18911", "--", *map(str, files), str(files[0])])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == f"Mounted 2 recordings in {tmp_path / 'Mounts'}"
    assert started == ([] if running else [18911])
    assert calls == ["/mounts", "/opened-clips", "/mounts", "/mounts"]


def test_mount_says_why_it_could_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "clip.mcraw"
    clip.touch()
    monkeypatch.setattr(module, "answers", lambda port: True)
    monkeypatch.setattr(
        module, "request", lambda *args, **kwargs: {"available": False, "reason": "FUSE unavailable"}
    )
    result = CliRunner().invoke(cli, ["mount", "--port", "18911", "--", str(clip)])
    assert result.exit_code == 1
    assert "FUSE unavailable" in result.output


IN_USE = "Media is in use; close its open files in the receiving application before unmounting"


def engine_with_mounts(
    monkeypatch: pytest.MonkeyPatch, mounts: list[dict[str, Any]], *, busy: tuple[str, ...] = ()
) -> list[str]:
    """An engine listing these mounts, refusing to unmount the `busy` ones; the unmounts asked of it."""
    asked: list[str] = []
    monkeypatch.setattr(module, "answers", lambda port: True)

    def request(port: int, path: str, method: str = "GET", body: Any = None, **kwargs: Any) -> dict[str, Any]:
        if path == "/mounts":
            assert method == "GET"
            return {"available": True, "mounts": mounts}
        assert method == "POST"
        asked.append(path)
        if path.split("/")[2] in busy:
            error = {"error": {"code": "engine-busy", "message": IN_USE}}
            raise urllib.error.HTTPError(
                "local", 409, "error", Message(), io.BytesIO(json.dumps(error).encode())
            )
        return {}

    monkeypatch.setattr(module, "request", request)
    return asked


def test_unmount_takes_every_mount_of_the_chosen_recordings_and_no_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The recordings need not exist: a mount can outlive its recording's move or deletion.
    first, second, other = (tmp_path / name for name in ("first.mcraw", "second.mcraw", "other.mcraw"))
    asked = engine_with_mounts(
        monkeypatch,
        [
            {"id": "first-full", "source": str(first), "active": True},
            {"id": "first-proxy", "source": str(first), "active": True},
            {"id": "first-old", "source": str(first), "active": False},
            {"id": "second", "source": str(second), "active": True},
            {"id": "other", "source": str(other), "active": True},
        ],
    )
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", str(first), str(second)])
    assert result.exit_code == 0, result.output
    assert asked == [
        "/mounts/first-full/unmount",
        "/mounts/first-proxy/unmount",
        "/mounts/second/unmount",
    ]
    assert "Unmounted 2 recordings. DaVinci Resolve can no longer read them." in result.output


def test_unmount_finds_a_recording_through_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recording, link = tmp_path / "recording.mcraw", tmp_path / "link.mcraw"
    recording.touch()
    link.symlink_to(recording)
    asked = engine_with_mounts(monkeypatch, [{"id": "one", "source": str(recording), "active": True}])
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", str(link)])
    assert result.exit_code == 0, result.output
    assert asked == ["/mounts/one/unmount"]
    assert "Unmounted 1 recording. DaVinci Resolve can no longer read it." in result.output


def test_media_still_in_use_stays_mounted_is_named_and_the_rest_are_unmounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first.mcraw", tmp_path / "second.mcraw"
    asked = engine_with_mounts(
        monkeypatch,
        [
            {"id": "first-full", "source": str(first), "active": True},
            {"id": "first-proxy", "source": str(first), "active": True},
            {"id": "second", "source": str(second), "active": True},
        ],
        busy=("first-proxy",),
    )
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", str(first), str(second)])
    assert result.exit_code == 1
    assert len(asked) == 3
    assert f"1 of 2 recordings still mounted. first.mcraw: {IN_USE}" in result.output.replace("\n", " ")


def test_one_recording_still_in_use_is_said_without_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mcraw"
    engine_with_mounts(monkeypatch, [{"id": "one", "source": str(clip), "active": True}], busy=("one",))
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", str(clip)])
    assert result.exit_code == 1
    assert f"Still mounted. clip.mcraw: {IN_USE}" in result.output.replace("\n", " ")


@pytest.mark.parametrize(
    ("names", "says"),
    [
        (["clip.mcraw"], "Nothing to unmount. clip.mcraw is not mounted."),
        (["one.mcraw", "two.mcraw"], "Nothing to unmount. The 2 chosen recordings are not mounted."),
    ],
)
def test_nothing_to_unmount_is_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str], says: str
) -> None:
    other = tmp_path / "other.mcraw"
    asked = engine_with_mounts(monkeypatch, [{"id": "other", "source": str(other), "active": True}])
    paths = [str(tmp_path / name) for name in names]
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", *paths])
    assert result.exit_code == 0, result.output
    assert asked == []
    assert says in result.output


def test_unmount_starts_no_engine_for_recordings_without_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[int] = []
    clip = tmp_path / "clip.mcraw"
    monkeypatch.setattr(module, "answers", lambda port: False)
    monkeypatch.setattr(module, "mounted_sources", lambda: {tmp_path / "another recording.mcraw"})
    monkeypatch.setattr(module, "start_server", lambda port, roots: started.append(port))
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", str(clip)])
    assert result.exit_code == 0, result.output
    assert started == []
    assert "Nothing to unmount. clip.mcraw is not mounted." in result.output


def test_unmount_starts_an_engine_for_media_that_outlived_the_last_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mcraw"
    asked = engine_with_mounts(monkeypatch, [{"id": "one", "source": str(clip), "active": True}])
    started: list[int] = []
    monkeypatch.setattr(module, "answers", lambda port: False)
    monkeypatch.setattr(module, "mounted_sources", lambda: {clip.resolve()})
    monkeypatch.setattr(module, "start_server", lambda port, roots: started.append(port))
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", str(clip)])
    assert result.exit_code == 0, result.output
    assert started == [18911]
    assert asked == ["/mounts/one/unmount"]


def test_unmount_says_when_it_cannot_reach_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(module, "answers", lambda port: True)
    monkeypatch.setattr(module, "request", request)
    result = CliRunner().invoke(cli, ["unmount", "--port", "18911", "--", str(tmp_path / "clip.mcraw")])
    assert result.exit_code == 1
    assert "Could not reach MCRAWsome's engine" in result.output
