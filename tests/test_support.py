import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from mcrawsome import support
from mcrawsome.cli.main import cli
from mcrawsome.support import LOG_LIMIT, make_bundle

MEDIA = (".mcraw", ".mp4", ".mov", ".mkv", ".webm", ".wav", ".dng", ".png", ".jpg", ".gcsv", ".cube")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The home folder is the test's folder, so every path the bundle meets is inside it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _sidecar(folder: Path) -> Path:
    path = folder / "out.mp4.mcrawsome.toml"
    path.write_text(f'[[inputs]]\npath = "{folder / "clip.mcraw"}"\n', encoding="utf-8")
    return path


def _members(bundle: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(bundle) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_bundle_has_the_expected_members(fake_ffmpeg: Path, home: Path, synthetic_video: Path) -> None:
    sidecar = _sidecar(home)
    out = home / "bundle.zip"
    names = make_bundle(out, [sidecar, synthetic_video])
    members = _members(out)
    assert sorted(names) == sorted(members)
    assert {"README.txt", "doctor.json", "system.json"} <= set(members)
    assert f"sidecars/{sidecar.name}" in members
    assert f"clips/{synthetic_video.name}.json" in members
    assert not any(name.startswith("jobs/") for name in members)
    system = json.loads(members["system.json"])
    wanted = {"os", "kernel", "cpu", "memory", "python", "mcrawsome", "dependencies", "ffmpeg"}
    assert wanted <= set(system)
    assert "numpy" in system["dependencies"]
    assert "ffmpeg version" in system["ffmpeg"]
    checks = json.loads(members["doctor.json"])["checks"]
    assert any(check["name"] == "FFmpeg" for check in checks)
    clip = json.loads(members[f"clips/{synthetic_video.name}.json"])
    assert clip["frames"] == 30
    assert "Video" in clip
    readme = members["README.txt"].decode()
    assert "no picture" in readme
    assert "sound" in readme


def test_no_member_is_media_or_larger_than_1_mb(fake_ffmpeg: Path, home: Path, synthetic_video: Path) -> None:
    out = home / "bundle.zip"
    make_bundle(out, [synthetic_video])
    with zipfile.ZipFile(out) as archive:
        for info in archive.infolist():
            assert not info.filename.lower().endswith(MEDIA), info.filename
            assert info.file_size <= LOG_LIMIT, info.filename
    assert out.stat().st_size < 200_000


def test_home_folder_becomes_a_tilde(fake_ffmpeg: Path, home: Path) -> None:
    sidecar = _sidecar(home)
    out = home / "bundle.zip"
    make_bundle(out, [sidecar])
    members = _members(out)
    text = members[f"sidecars/{sidecar.name}"].decode()
    assert 'path = "~/clip.mcraw"' in text
    for name, data in members.items():
        assert str(home).encode() not in data, name


@pytest.mark.fixture
def test_bundle_of_the_10bit_clip_is_small(
    fake_ffmpeg: Path, home: Path, fixture_10bit_rotated: Path
) -> None:
    out = home / "bundle.zip"
    make_bundle(out, [fixture_10bit_rotated])
    assert out.stat().st_size < 200_000
    clip = json.loads(_members(out)[f"clips/{fixture_10bit_rotated.name}.json"])
    locations = [
        section["Location"]
        for section in clip.values()
        if isinstance(section, dict) and "Location" in section
    ]
    assert locations in (["not recorded"], ["recorded (left out of this bundle)"])


def test_cli_writes_a_bundle_and_refuses_to_overwrite(
    fake_ffmpeg: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    sidecar = _sidecar(home)
    result = CliRunner().invoke(cli, ["support-bundle", str(sidecar)])
    assert result.exit_code == 0, result.output
    made = list(home.glob("mcrawsome-support-*.zip"))
    assert len(made) == 1
    assert made[0].name[len("mcrawsome-support-") : -len(".zip")].replace("-", "").isdigit()
    assert "README.txt" in result.output
    assert "before sharing" in result.output
    out = home / "mine.zip"
    first = CliRunner().invoke(cli, ["support-bundle", "--output", str(out)])
    assert first.exit_code == 0, first.output
    out.write_bytes(b"precious")
    again = CliRunner().invoke(cli, ["support-bundle", "--output", str(out)])
    assert again.exit_code != 0
    assert "already exists" in again.output
    assert out.read_bytes() == b"precious"
    forced = CliRunner().invoke(cli, ["support-bundle", "--output", str(out), "--force"])
    assert forced.exit_code == 0, forced.output
    assert zipfile.is_zipfile(out)


def test_a_recording_location_is_left_out(
    fake_ffmpeg: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome import support

    clip = home / "located.mcraw"
    clip.write_bytes(b"x")
    unlocated = home / "unlocated.mcraw"
    unlocated.write_bytes(b"x")

    def described(path: Path) -> dict[str, object]:
        location = "51.507351, -0.127758, 11 m" if path == clip else "not recorded"
        return {"clip": str(path), "frames": 3, "Recording": {"Gyro": "none", "Location": location}}

    monkeypatch.setattr(support, "describe_any", described)
    out = home / "bundle.zip"
    make_bundle(out, [clip, unlocated])
    members = _members(out)
    located = members[f"clips/{clip.name}.json"].decode()
    assert "51.507" not in located
    assert "-0.127" not in located
    assert json.loads(located)["Recording"]["Location"] == "recorded (left out of this bundle)"
    assert json.loads(members[f"clips/{unlocated.name}.json"])["Recording"]["Location"] == "not recorded"
    assert "location" in members["README.txt"].decode().lower()


def test_a_symlinked_home_folder_is_replaced_both_ways(
    fake_ffmpeg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))
    sidecar = real / "out.mp4.mcrawsome.toml"
    sidecar.write_text(f'one = "{link / "a.mcraw"}"\ntwo = "{real / "b.mcraw"}"\n', encoding="utf-8")
    out = real / "bundle.zip"
    make_bundle(out, [sidecar])
    members = _members(out)
    text = members[f"sidecars/{sidecar.name}"].decode()
    assert 'one = "~/a.mcraw"' in text
    assert 'two = "~/b.mcraw"' in text
    for name, data in members.items():
        assert str(real).encode() not in data, name
        assert str(link).encode() not in data, name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/home/thom/x", "~/x"),
        ("/home/thom", "~"),
        ('path = "/home/thom/a"', 'path = "~/a"'),
        ("path='/home/thom/a'", "path='~/a'"),
        ("HOME=/home/thom", "HOME=~"),
        ("PATH=/usr/bin:/home/thom/bin", "PATH=/usr/bin:~/bin"),
        ("(/home/thom/a) [/home/thom/b], /home/thom/c", "(~/a) [~/b], ~/c"),
        ("/data/home/thom/x", "/data/home/thom/x"),
        ("x/home/thom/y", "x/home/thom/y"),
        ("/home/thomas/x", "/home/thomas/x"),
    ],
)
def test_home_is_replaced_only_where_it_starts_a_path(
    text: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.support import _private

    monkeypatch.setenv("HOME", "/home/thom")
    assert _private(text) == expected


def test_two_given_sidecars_with_the_same_name_are_both_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Avoid hardware probes: this case tests member selection, not doctor.
    monkeypatch.setattr(support, "run_checks", list)
    monkeypatch.setattr(support, "system_info", dict)
    sidecars = []
    for folder_name, content in [("first", "first export"), ("second", "second export")]:
        folder = tmp_path / folder_name
        folder.mkdir()
        sidecar = folder / "out.mov.mcrawsome.toml"
        sidecar.write_text(f'note = "{content}"\n')
        sidecars.append(sidecar)
    members = support.bundle_members(sidecars)
    texts = [text for name, text in members.items() if name.startswith("sidecars/")]
    assert len(texts) == 2, members
    assert any("first export" in text for text in texts)
    assert any("second export" in text for text in texts)


@pytest.mark.parametrize("character", ["é", "€", "😀"])
def test_an_unbroken_unicode_log_respects_the_member_byte_limit(character: str) -> None:
    text = character * (support.LOG_LIMIT + 1)
    clipped = support._tail(text)
    assert len(clipped.encode("utf-8")) <= support.LOG_LIMIT
    assert clipped.endswith(character * 10)


def test_tail_preserves_a_short_unicode_log() -> None:
    assert support._tail("é € 😀\n") == "é € 😀\n"


def test_two_given_clips_with_the_same_name_are_both_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(support, "run_checks", list)
    monkeypatch.setattr(support, "system_info", dict)
    monkeypatch.setattr(support, "_clip_document", lambda clip: {"source_folder": clip.parent.name})
    paths = [tmp_path / "first/clip.mcraw", tmp_path / "second/clip.mcraw"]
    members = support.bundle_members(paths)
    documents = [text for name, text in members.items() if name.startswith("clips/")]
    assert len(documents) == 2
    assert any('"first"' in text for text in documents)
    assert any('"second"' in text for text in documents)
