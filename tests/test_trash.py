"""Recordings go to the desktop's Trash with restore information, never deleted."""

import os
from datetime import datetime
from pathlib import Path

import pytest

from mcrawsome.trash import companions, move_to_trash, split_parts, trash_for


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path / "data"


def test_home_trash_keeps_restore_information(tmp_path: Path, data_home: Path) -> None:
    clip = tmp_path / "clips" / "a b.mcraw"
    clip.parent.mkdir()
    clip.write_bytes(b"raw")
    moved = move_to_trash(clip, now=datetime(2026, 10, 28, 9, 30, 5))
    assert moved == data_home / "Trash/files/a b.mcraw"
    assert moved.read_bytes() == b"raw"
    assert not clip.exists()
    info = (data_home / "Trash/info/a b.mcraw.trashinfo").read_text()
    assert info == f"[Trash Info]\nPath={str(clip).replace(' ', '%20')}\nDeletionDate=2026-10-28T09:30:05\n"


def test_names_already_in_the_trash_are_kept(tmp_path: Path, data_home: Path) -> None:
    for content in (b"first", b"second", b"third"):
        clip = tmp_path / "a.mcraw"
        clip.write_bytes(content)
        move_to_trash(clip)
    files = data_home / "Trash/files"
    assert {path.name: path.read_bytes() for path in files.iterdir()} == {
        "a.mcraw": b"first",
        "a.2.mcraw": b"second",
        "a.3.mcraw": b"third",
    }
    assert sorted(path.name for path in (data_home / "Trash/info").iterdir()) == [
        "a.2.mcraw.trashinfo",
        "a.3.mcraw.trashinfo",
        "a.mcraw.trashinfo",
    ]


def test_a_failed_move_leaves_no_information_behind(
    tmp_path: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"raw")

    def refuse(*_: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "rename", refuse)
    with pytest.raises(PermissionError):
        move_to_trash(clip)
    assert clip.exists()
    assert not list((data_home / "Trash/info").iterdir())


def test_other_devices_use_their_own_top_folder(
    tmp_path: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = tmp_path / "card"
    clip = card / "DCIM" / "a.mcraw"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"raw")
    real_lstat = Path.lstat
    real_stat = Path.stat

    def device(path: Path, info: os.stat_result) -> os.stat_result:
        on_card = path == card or card in path.parents
        values = list(info)
        values[2] = 99 if on_card else info.st_dev
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", lambda path: device(path, real_lstat(path)))
    monkeypatch.setattr(Path, "stat", lambda path, **kw: device(path, real_stat(path, **kw)))
    trash, top = trash_for(clip)
    assert (trash, top) == (card / f".Trash-{os.getuid()}", card)
    moved = move_to_trash(clip)
    assert moved == trash / "files/a.mcraw"
    assert "Path=DCIM/a.mcraw\n" in (trash / "info/a.mcraw.trashinfo").read_text()


def test_companions_are_the_files_written_for_the_recording(tmp_path: Path) -> None:
    clip = tmp_path / "261004_174122_VIDEO_26mm.mcraw"
    for name in (
        clip.name,
        clip.stem + ".gcsv",
        clip.stem + ".meta",
        clip.stem + ".mcrawsome.toml",
        "other.gcsv",
    ):
        (tmp_path / name).write_text("x")
    assert [path.name for path in companions(clip)] == [
        clip.name,
        clip.stem + ".gcsv",
        clip.stem + ".meta",
        clip.stem + ".mcrawsome.toml",
    ]
    video = tmp_path / "v.mp4"
    (tmp_path / "v.mp4.csv").write_text("x")
    assert [path.name for path in companions(video)] == ["v.mp4", "v.mp4.csv"]


def test_other_parts_of_a_split_recording_are_found(tmp_path: Path) -> None:
    for name in ("a.0.mcraw", "a.1.mcraw", "a.2.MCRAW", "ab.1.mcraw", "a.mcraw", "b.0.mcraw"):
        (tmp_path / name).write_bytes(b"")
    assert split_parts([tmp_path / "a.0.mcraw"]) == [tmp_path / "a.1.mcraw", tmp_path / "a.2.MCRAW"]
    assert split_parts([tmp_path / "a.0.mcraw", tmp_path / "a.1.mcraw", tmp_path / "a.2.MCRAW"]) == []
    assert split_parts([tmp_path / "a.mcraw"]) == []
