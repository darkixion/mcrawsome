"""Records of media that is no longer mounted are logged and removed; live ones stay."""

import json
import os
from pathlib import Path

import pytest

from mcrawsome.errors import McError
from mcrawsome.mount import manager as module
from mcrawsome.mount.manager import MountedSequence, MountManager, process_start, save_record
from mcrawsome.mount.sequence import source_stamp
from mcrawsome.paths import state_dir


def _record(manager: MountManager, identity: str, source: Path) -> Path:
    folder = manager.root / identity
    folder.mkdir(parents=True)
    mount = MountedSequence(
        id=identity, source=source, path=folder / "media", stamp=source_stamp(source), sent=True
    )
    save_record(folder, mount)
    return folder


def test_listing_logs_and_removes_inactive_records_and_keeps_starting_ones(tiny_raw: Path) -> None:
    manager = MountManager()
    stale = _record(manager, "a" * 24, tiny_raw)
    busy = _record(manager, "b" * 24, tiny_raw)
    (busy / "starting.json").write_text(json.dumps({"pid": os.getpid(), "start": process_start(os.getpid())}))
    leftover = manager.root / ("c" * 24)
    leftover.mkdir()  # a folder without a record, from an interrupted start
    listed = manager.listing().mounts
    assert [mount.id for mount in listed] == ["b" * 24]
    assert not stale.exists()
    assert not leftover.exists()
    assert busy.exists()
    entries = [json.loads(line) for line in (state_dir() / "mount-history.log").read_text().splitlines()]
    assert {entry["id"] for entry in entries} == {"a" * 24, "c" * 24}
    logged = next(entry for entry in entries if entry["id"] == "a" * 24)
    assert logged["sent"] is True
    assert logged["source"] == str(tiny_raw)
    assert "removed" in logged
    assert "active" not in logged


def test_the_history_moves_aside_when_it_reaches_its_limit(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "HISTORY_LIMIT", 10)
    manager = MountManager()
    history = state_dir() / "mount-history.log"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("x" * 20 + "\n")
    _record(manager, "d" * 24, tiny_raw)
    manager.listing()
    assert history.with_name("mount-history.log.1").read_text() == "x" * 20 + "\n"
    assert json.loads(history.read_text())["id"] == "d" * 24


def test_sent_media_is_claimed_at_start_up_and_mounted_again(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = MountManager()
    sent = _record(manager, "a" * 24, tiny_raw)
    record = MountedSequence.model_validate_json((sent / "sequence.json").read_bytes())
    save_record(sent, record.model_copy(update={"retained": True}))
    _record(manager, "b" * 24, tiny_raw)  # mounted but never sent: nothing names it
    moved = tmp_path / "moved.mcraw"
    moved.write_bytes(tiny_raw.read_bytes())
    gone = _record(manager, "c" * 24, moved)
    save_record(
        gone,
        MountedSequence.model_validate_json((gone / "sequence.json").read_bytes()).model_copy(
            update={"retained": True}
        ),
    )
    moved.unlink()
    claimed = manager.sent_offline()
    assert claimed == ["a" * 24]
    manager.listing()  # a listing before the remount must not prune what start-up claimed
    assert sent.exists()
    assert not (manager.root / ("b" * 24)).exists()
    remounted: list[str] = []

    def remount(self: MountManager, identity: str) -> MountedSequence:
        remounted.append(identity)
        if identity == "f" * 24:
            raise McError("offline", code="input-unreadable")
        return record

    monkeypatch.setattr(MountManager, "remount", remount)
    assert manager.restore([*claimed, "f" * 24]) == [record]
    assert remounted == ["a" * 24, "f" * 24]
    assert manager.restoring == set()
