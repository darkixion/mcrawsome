"""Build approved test fixtures or fetch the archive described by the manifest."""

import argparse
import gzip
import hashlib
import os
import shutil
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import make_fixtures

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "manifest.toml"
DATA = MANIFEST.parent / "data"
ARCHIVE = "mcstudio-fixtures-v1.tar.gz"


def _sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def build(source_dir: Path, manifest: Path = MANIFEST, out_dir: Path | None = None) -> Path:
    destination = (out_dir or Path.cwd()) / ARCHIVE
    with tempfile.TemporaryDirectory(prefix="mcrawsome-fixtures-build-") as scratch:
        files = make_fixtures.create_fixtures(source_dir, Path(scratch))
        with (
            destination.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w") as archive,
        ):
            for path in files:
                member = archive.gettarinfo(str(path), arcname=path.name)
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ""
                member.mode = 0o644
                with path.open("rb") as file:
                    archive.addfile(member, file)
    digest = _sha256(destination)
    names = ", ".join(f'"{path.name}"' for path in files)
    manifest.write_text(
        "# Only owner-approved fixtures; set url after uploading the release asset.\n"
        f'archive = "{ARCHIVE}"\nurl = ""\nsha256 = "{digest}"\nmembers = [{names}]\n'
    )
    print(f"{destination}: {destination.stat().st_size} bytes, SHA-256 {digest}")
    return destination


def _plain_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and Path(name).name == name and "\\" not in name


def _request(url: str, token: str | None) -> urllib.request.Request:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"} if token else {})
    if token:  # unredirected: never copied to the signed URL on another host
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def fetch(manifest: Path = MANIFEST, out_dir: Path = DATA) -> None:
    with manifest.open("rb") as file:
        config = tomllib.load(file)
    url = os.environ.get("MCRAWSOME_FIXTURES_URL") or config["url"]
    if not url:
        print("no fixture URL configured; fixture tests will skip when their clips are missing")
        return
    names = config["members"]
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or not _plain_name(name) for name in names)
        or len(names) != len(set(names))
    ):
        raise ValueError("manifest member names must be unique plain filenames")
    request = _request(url, os.environ.get("MCRAWSOME_FIXTURES_TOKEN"))
    with tempfile.TemporaryDirectory(prefix="mcrawsome-fixtures-fetch-") as scratch:
        folder = Path(scratch)
        download = folder / ARCHIVE
        with urllib.request.urlopen(request) as response, download.open("wb") as file:
            shutil.copyfileobj(response, file)
        if _sha256(download).lower() != str(config["sha256"]).lower():
            raise ValueError("fixture archive SHA-256 mismatch; nothing extracted")
        with tarfile.open(download, "r:gz") as archive:
            members = archive.getmembers()
            seen = set()
            for member in members:
                if not _plain_name(member.name) or not member.isfile() or member.name in seen:
                    raise ValueError("unsafe or duplicate archive member; nothing extracted")
                seen.add(member.name)
            if not set(names) <= seen:
                raise ValueError("listed archive member missing; nothing extracted")
            if any(path.is_symlink() for path in (out_dir, *out_dir.parents)):
                raise ValueError("fixture output directory must not contain a symlink")
            for name in names:
                destination = out_dir / name
                if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                    raise ValueError("fixture output member must be a plain file, never a symlink")
                source = archive.extractfile(name)
                if source is None:
                    raise ValueError("archive member has no file contents")
                with source, (folder / name).open("wb") as file:
                    shutil.copyfileobj(source, file)
            out_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                shutil.copyfile(folder / name, out_dir / name)
    print(f"fetched {len(names)} fixture files into {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build").add_argument("source_dir", type=Path)
    commands.add_parser("fetch")
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(args.source_dir)
        else:
            fetch()
    except urllib.error.URLError:
        parser.exit(1, "fixture download failed\n")
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"fixture operation failed: {error}\n")


if __name__ == "__main__":
    main()
