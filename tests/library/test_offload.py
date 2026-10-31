"""Fast Copy: recordings offloaded from a card or phone folder, hashed, verified and resumable."""

import hashlib
import os
from pathlib import Path

import pytest

from mcrawsome.errors import McError
from mcrawsome.library import offload
from mcrawsome.library.offload import CopyProgress, find_recordings, offload_recordings


def _write(path: Path, size: int, seed: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes((seed + i * 7) % 251 for i in range(size)))
    return path


@pytest.fixture
def card(tmp_path: Path) -> Path:
    root = tmp_path / "card" / "DCIM" / "MotionCam"
    _write(root / "A.mcraw", 5000)
    _write(root / "A.gcsv", 300, 1)
    _write(root / "A.json", 40, 2)
    _write(root / "A.mcrawsome.toml", 20, 3)
    _write(root / "B.0.mcraw", 4096, 4)
    _write(root / "B.1.mcraw", 1000, 5)
    _write(root / "B.gcsv", 100, 6)
    _write(root / "notes.txt", 10, 7)
    (root / "A_timeseries").mkdir()
    _write(root / "A_timeseries" / "gyro.csv", 10, 8)
    _write(root / "older" / "C.mcraw", 700, 9)
    _write(root / ".hidden" / "D.mcraw", 70, 10)
    return tmp_path / "card"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_recordings_are_found_with_their_split_parts_and_sidecars(card: Path) -> None:
    found = {r.name: [p.name for p in r.files] for r in find_recordings([card])}
    assert found == {
        "A": ["A.gcsv", "A.json", "A.mcraw", "A.mcrawsome.toml"],
        "B": ["B.0.mcraw", "B.1.mcraw", "B.gcsv"],
        "C": ["C.mcraw"],
    }
    recordings = find_recordings([card / "DCIM" / "MotionCam" / "B.1.mcraw"])
    assert [(r.name, r.size) for r in recordings] == [("B", 4096 + 1000 + 100)]


def test_a_copy_is_verified_keeps_dates_and_leaves_no_partial_files(card: Path, tmp_path: Path) -> None:
    destination = tmp_path / "library"
    seen: list[CopyProgress] = []
    results = offload_recordings(find_recordings([card]), destination, progress=seen.append)
    assert {r.status for r in results} == {"copied"}
    for result in results:
        assert result.target.parent == destination
        assert _sha(result.target) == _sha(result.source) == result.sha256
        assert result.target.stat().st_mtime_ns == result.source.stat().st_mtime_ns
    assert not list(destination.glob("*.part"))
    assert sorted(p.name for p in destination.iterdir()) == sorted(
        ["A.gcsv", "A.json", "A.mcraw", "A.mcrawsome.toml", "B.0.mcraw", "B.1.mcraw", "B.gcsv", "C.mcraw"]
    )
    copied = [p.done_bytes for p in seen]
    assert copied == sorted(copied)
    assert seen[-1].done_bytes == seen[-1].total_bytes == sum(r.source.stat().st_size for r in results)
    assert seen[-1].files_done == seen[-1].files_total == 8


def test_copying_again_skips_identical_files_without_rewriting_them(card: Path, tmp_path: Path) -> None:
    destination = tmp_path / "library"
    offload_recordings(find_recordings([card]), destination)
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in destination.iterdir()}
    results = offload_recordings(find_recordings([card]), destination)
    assert {r.status for r in results} == {"skipped"}
    assert {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in destination.iterdir()} == before


def test_a_different_file_at_the_destination_is_never_replaced(card: Path, tmp_path: Path) -> None:
    destination = tmp_path / "library"
    other = _write(destination / "A.mcraw", 5000, 99)
    results = {r.target.name: r for r in offload_recordings(find_recordings([card]), destination)}
    assert results["A.mcraw"].status == "failed"
    assert "different" in results["A.mcraw"].message
    assert other.read_bytes() == bytes((99 + i * 7) % 251 for i in range(5000))
    assert results["A.gcsv"].status == "copied"


def test_a_cancelled_copy_leaves_a_part_file_and_resumes_from_its_last_good_block(
    card: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(offload, "CHUNK", 1024)
    destination = tmp_path / "library"
    recordings = find_recordings([card / "DCIM" / "MotionCam" / "A.mcraw"])
    calls = iter(range(1000))
    results = offload_recordings(recordings, destination, stop=lambda: next(calls) >= 5)
    assert "cancelled" in {r.status for r in results}
    part = destination / "A.mcraw.part"
    assert part.exists()
    assert not (destination / "A.mcraw").exists()
    kept = part.stat().st_size
    assert 0 < kept < 5000
    # A crash can leave the end of a part file unwritten: the block holding it is copied again.
    with part.open("r+b") as file:
        file.seek(kept - 10)
        file.write(b"\0" * 10)
    written: list[int] = []
    real = os.write

    def counting(fd: int, data: bytes) -> int:
        written.append(len(data))
        return real(fd, data)

    monkeypatch.setattr(os, "write", counting)
    results = offload_recordings(recordings, destination)
    a = next(r for r in results if r.target.name == "A.mcraw")
    assert a.status == "copied"
    assert _sha(destination / "A.mcraw") == _sha(a.source)
    assert not part.exists()
    resumed = 5000 - (kept // 1024 - 1) * 1024  # from the start of the damaged block
    others = sum(
        r.source.stat().st_size for r in results if r.status == "copied" and r.target.name != "A.mcraw"
    )
    assert sum(written) - others == resumed


def test_a_copy_that_does_not_verify_is_reported_and_removed(
    card: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "library"
    monkeypatch.setattr(offload, "_hash_file", lambda path: "0" * 64)
    results = offload_recordings(find_recordings([card / "DCIM" / "MotionCam" / "A.mcraw"]), destination)
    assert {r.status for r in results} == {"failed"}
    assert all("verify" in r.message for r in results)
    assert not list(destination.iterdir())


def test_the_same_name_from_two_folders_is_copied_once(tmp_path: Path) -> None:
    _write(tmp_path / "one" / "X.mcraw", 100, 1)
    _write(tmp_path / "two" / "X.mcraw", 100, 2)
    results = offload_recordings(find_recordings([tmp_path / "one", tmp_path / "two"]), tmp_path / "library")
    assert [r.status for r in results] == ["copied", "failed"]
    assert "different" in results[1].message


def test_a_missing_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(McError, match="not found") as caught:
        find_recordings([tmp_path / "nowhere"])
    assert caught.value.code == "input-missing"


def test_the_copy_command_copies_and_reports_each_file(card: Path, tmp_path: Path) -> None:
    import json

    from click.testing import CliRunner

    from mcrawsome.cli.main import cli

    destination = tmp_path / "library"
    result = CliRunner().invoke(cli, ["copy", str(card), str(destination), "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert {r["status"] for r in document["files"]} == {"copied"}
    assert document["recordings"] == 3
    again = CliRunner().invoke(cli, ["copy", str(card), str(destination)])
    assert again.exit_code == 0
    assert "8 already copied" in again.output


def test_the_copy_command_fails_when_a_file_could_not_be_copied(card: Path, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from mcrawsome.cli.main import cli

    destination = tmp_path / "library"
    _write(destination / "A.json", 40, 77)
    result = CliRunner().invoke(cli, ["copy", str(card), str(destination)])
    assert result.exit_code == 1
    assert "A.json" in result.output
    assert "different" in result.output
