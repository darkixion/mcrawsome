from pathlib import Path

import pytest

from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.readiness import readable_media


@pytest.mark.parametrize("bad", ["missing", "empty", "wrong-header", "short-header"])
def test_live_mount_flags_do_not_hide_unreadable_last_frame(tmp_path: Path, bad: str) -> None:
    mount = MountedSequence(
        id="test",
        source=tmp_path / "source.mcraw",
        path=tmp_path,
        stamp=(1, 2, 3, 4),
        frames=3,
        active=True,
        serving=True,
    )
    header = b"II*\0\x08\0\0\0" + b"\0" * 100
    (tmp_path / "source-000000.dng").write_bytes(header)
    last = tmp_path / "source-000002.dng"
    last.write_bytes(header)
    assert readable_media(mount) == len(header)
    if bad == "missing":
        last.unlink()
    else:
        last.write_bytes({"empty": b"", "wrong-header": b"not TIFF" * 20, "short-header": b"II*\0"}[bad])
    with pytest.raises(ValueError, match=r"source-000002.*Remount"):
        readable_media(mount)
