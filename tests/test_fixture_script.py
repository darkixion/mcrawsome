import hashlib
import importlib.util
import io
import tarfile
import tomllib
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from types import ModuleType

import pytest

from mcrawsome.io.container import ContainerWriter, read_index

ROOT = Path(__file__).resolve().parents[1]
MEMBERS = ["clip_10bit_rotated.mcraw", "clip_10bit_rotated.gcsv"]


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.delenv("MCRAWSOME_FIXTURES_URL", raising=False)
    monkeypatch.delenv("MCRAWSOME_FIXTURES_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("fixtures", ROOT / "scripts" / "fixtures.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(tmp_path: Path, extra: tarfile.TarInfo | None = None) -> Path:
    path = tmp_path / "fixtures.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name in [*MEMBERS, "unlisted.txt"]:
            data = name.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if extra is not None:
            archive.addfile(extra)
    return path


def _manifest(tmp_path: Path, archive: Path, *, sha256: str | None = None, url: str | None = None) -> Path:
    path = tmp_path / "manifest.toml"
    digest = sha256 if sha256 is not None else hashlib.sha256(archive.read_bytes()).hexdigest()
    location = archive.as_uri() if url is None else url
    path.write_text(f'url = "{location}"\nsha256 = "{digest}"\nmembers = {MEMBERS!r}\n')
    return path


def test_build_refuses_an_unapproved_source(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script.make_fixtures, "FIXTURES", {MEMBERS[0]: ("unapproved.mcraw", 0, 3)})
    with pytest.raises(ValueError, match="approved"):
        script.build(tmp_path, tmp_path / "manifest.toml", tmp_path)
    assert not list(tmp_path.iterdir())


def test_build_trims_only_the_approved_clip_and_records_the_hash(script: ModuleType, tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "261004_174122_VIDEO_26mm.mcraw"
    with ContainerWriter(source, {}) as writer:
        for index in range(105):
            writer.add_frame(index * 40_000_000, b"frame", b"{}")
    source.with_suffix(".gcsv").write_text("gyro data")
    (source_dir / "unapproved.mcraw").write_bytes(b"must never be read")
    manifest = tmp_path / "manifest.toml"
    archive_path = script.build(source_dir, manifest, tmp_path)
    assert archive_path.name == "mcstudio-fixtures-v1.tar.gz"
    data = tomllib.loads(manifest.read_text())
    assert data["url"] == ""
    assert data["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert data["members"] == MEMBERS
    out = tmp_path / "data"
    manifest.write_text(manifest.read_text().replace('url = ""', f'url = "{archive_path.as_uri()}"'))
    script.fetch(manifest, out)
    with (out / MEMBERS[0]).open("rb") as clip:
        assert [frame.timestamp_ns for frame in read_index(clip).frames] == [
            4_000_000_000,
            4_040_000_000,
            4_080_000_000,
        ]
    assert (out / MEMBERS[1]).read_text() == "gyro data"


def test_fetch_extracts_only_listed_plain_files(script: ModuleType, tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    out = tmp_path / "data"
    script.fetch(_manifest(tmp_path, archive), out)
    assert sorted(path.name for path in out.iterdir()) == sorted(MEMBERS)
    for name in MEMBERS:
        assert (out / name).read_bytes() == name.encode()
        assert not (out / name).is_symlink()


def test_fetch_wrong_hash_extracts_nothing(script: ModuleType, tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    out = tmp_path / "data"
    with pytest.raises(ValueError, match="SHA-256"):
        script.fetch(_manifest(tmp_path, archive, sha256="0" * 64), out)
    assert not out.exists()


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "hardlink", "directory", "duplicate"])
def test_fetch_refuses_unsafe_archive_members(script: ModuleType, tmp_path: Path, kind: str) -> None:
    extra = tarfile.TarInfo(
        "../evil" if kind == "traversal" else "/evil" if kind == "absolute" else MEMBERS[0]
    )
    if kind in {"symlink", "hardlink"}:
        extra.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
        extra.linkname = "../evil"
    elif kind == "directory":
        extra.type = tarfile.DIRTYPE
    archive = _archive(tmp_path, extra)
    out = tmp_path / "data"
    with pytest.raises(ValueError, match="member"):
        script.fetch(_manifest(tmp_path, archive), out)
    assert not out.exists()
    assert not (tmp_path / "evil").exists()


def test_fetch_with_no_url_skips_the_download(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _archive(tmp_path)
    out = tmp_path / "data"
    script.fetch(_manifest(tmp_path, archive, url=""), out)
    assert "no fixture URL" in capsys.readouterr().out
    assert not out.exists()


def test_fetch_token_headers_and_url_override(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _archive(tmp_path)
    monkeypatch.setenv("MCRAWSOME_FIXTURES_TOKEN", "private-test-token")
    monkeypatch.setenv(
        "MCRAWSOME_FIXTURES_URL", "https://api.github.com/repos/owner/repo/releases/assets/123"
    )
    requests: list[urllib.request.Request] = []

    def open_archive(request: urllib.request.Request) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(archive.read_bytes())

    monkeypatch.setattr(script.urllib.request, "urlopen", open_archive)
    script.fetch(_manifest(tmp_path, archive, url=""), tmp_path / "data")
    assert len(requests) == 1
    assert requests[0].full_url == "https://api.github.com/repos/owner/repo/releases/assets/123"
    assert requests[0].get_header("Authorization") == "Bearer private-test-token"
    assert requests[0].get_header("Accept") == "application/octet-stream"
    captured = capsys.readouterr()
    assert "private-test-token" not in captured.out + captured.err


def test_fetch_refuses_a_symlink_destination(script: ModuleType, tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    out = tmp_path / "data"
    out.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        script.fetch(_manifest(tmp_path, archive), out)
    assert not list(other.iterdir())


def _write_sources(source_dir: Path) -> None:
    source_dir.mkdir()
    for stem in ("261004_174122_VIDEO_26mm", "261008_095010_VIDEO_25mm"):
        source = source_dir / f"{stem}.mcraw"
        with ContainerWriter(source, {}) as writer:
            for index in range(205):
                writer.add_frame(index * 40_000_000, b"frame", b"{}")
        source.with_suffix(".gcsv").write_text("gyro data")


def test_build_never_packs_a_local_only_fixture(script: ModuleType, tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_sources(source_dir)
    assert "clip_12bit.mcraw" in script.make_fixtures.LOCAL_ONLY
    manifest = tmp_path / "manifest.toml"
    archive_path = script.build(source_dir, manifest, tmp_path)
    assert tomllib.loads(manifest.read_text())["members"] == MEMBERS
    with tarfile.open(archive_path) as archive:
        assert sorted(archive.getnames()) == sorted(MEMBERS)


def test_local_creation_includes_the_local_only_fixtures(script: ModuleType, tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_sources(source_dir)
    files = script.make_fixtures.create_fixtures(source_dir, tmp_path / "out", include_local=True)
    assert {path.name for path in files} >= {"clip_12bit.mcraw", "clip_10bit_rotated.mcraw"}


def test_fetch_never_sends_the_token_to_a_redirect_target(script: ModuleType) -> None:
    request = script._request("https://api.github.com/repos/o/r/releases/assets/1", "secret-token")
    assert request.get_header("Authorization") == "Bearer secret-token"
    redirected = urllib.request.HTTPRedirectHandler().redirect_request(
        request, io.BytesIO(), 302, "Found", HTTPMessage(), "https://objects.example.net/signed?x=1"
    )
    assert redirected is not None
    assert redirected.full_url == "https://objects.example.net/signed?x=1"
    assert not redirected.has_header("Authorization")
    assert redirected.get_header("Accept") == "application/octet-stream"


def test_fetch_compares_the_hash_case_insensitively(script: ModuleType, tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest().upper()
    out = tmp_path / "data"
    script.fetch(_manifest(tmp_path, archive, sha256=digest), out)
    assert (out / MEMBERS[0]).exists()


@pytest.mark.parametrize("kind", [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_fetch_refuses_fifo_and_device_members(script: ModuleType, tmp_path: Path, kind: bytes) -> None:
    extra = tarfile.TarInfo("device")
    extra.type = kind
    archive = _archive(tmp_path, extra)
    out = tmp_path / "data"
    with pytest.raises(ValueError, match="member"):
        script.fetch(_manifest(tmp_path, archive), out)
    assert not out.exists()
