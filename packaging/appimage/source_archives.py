"""Copy or fetch the exact FFmpeg and dav1d source archives that build_ffmpeg.sh built from.

Usage: source_archives.py MANIFEST FOLDER, where MANIFEST is the ffmpeg-manifest.toml beside the build.
"""

import hashlib
import shutil
import sys
import tomllib
import urllib.request
from pathlib import Path

manifest = tomllib.loads(Path(sys.argv[1]).read_text())
output = Path(sys.argv[2])
output.mkdir(parents=True, exist_ok=True)
for component in manifest["components"]:
    url = component["source_url"]
    extension = "tar.xz" if url.endswith(".tar.xz") else "tar.gz"
    name = f"{component['name']}-{component['version']}.{extension}"
    archive = output / name
    cached = Path.home() / ".cache/mcrawsome-ffmpeg/sources" / name
    if not archive.exists():
        if cached.exists():
            shutil.copy2(cached, archive)
        else:
            urllib.request.urlretrieve(url, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != component["sha256"]:
        raise SystemExit(f"Source archive checksum mismatch: {archive}")
