"""Headless sends reuse saved mount settings and never create/replace a window or remove media."""

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
