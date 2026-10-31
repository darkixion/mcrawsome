"""Create the real-clip test fixtures from recordings the owner has approved.

Usage: uv run python scripts/make_fixtures.py <folder with the source .mcraw files>
Outputs go to tests/fixtures/data/ (never committed).
"""

import shutil
import sys
from pathlib import Path

APPROVED = {"261004_174122_VIDEO_26mm"}
FIXTURES = {
    "clip_10bit_rotated.mcraw": ("261004_174122_VIDEO_26mm.mcraw", 100, 3),
}
# Not approved for sharing: created on a developer's machine only, never packed, committed or uploaded.
LOCAL_ONLY = {
    "clip_12bit.mcraw": ("261008_095010_VIDEO_25mm.mcraw", 200, 3),
}


def create_fixtures(source_dir: Path, out_dir: Path, include_local: bool = False) -> list[Path]:
    table = {**FIXTURES, **(LOCAL_ONLY if include_local else {})}
    for name, (source, _, _) in table.items():
        path = source_dir / source
        if path.is_symlink() or (name in FIXTURES and Path(source).stem not in APPROVED):
            raise ValueError(f"source {source} is not an approved plain file")
        if not path.is_file():
            raise ValueError(f"approved source {source} missing")
        gyro = path.with_suffix(".gcsv")
        if gyro.is_symlink() or not gyro.is_file():
            raise ValueError(f"approved gyro log {gyro.name} missing or not a plain file")
    if out_dir.is_symlink():
        raise ValueError("fixture output directory must not be a symlink")

    from mcrawsome.io.trim import trim

    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for name, (source, start, count) in table.items():
        dst = out_dir / name
        if dst.is_symlink() or dst.with_suffix(".gcsv").is_symlink():
            raise ValueError("fixture output files must not be symlinks")
        if not dst.exists():
            trim(source_dir / source, dst, start, count)
        gyro = (source_dir / source).with_suffix(".gcsv")
        if not dst.with_suffix(".gcsv").exists():  # the clip keeps its timestamps
            shutil.copy(gyro, dst.with_suffix(".gcsv"))
        print(f"{dst} ({dst.stat().st_size / 1e6:.1f} MB)")
        files.extend([dst, dst.with_suffix(".gcsv")])
    return files


def main() -> None:
    source_dir = Path(sys.argv[1])
    out_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "data"
    create_fixtures(source_dir, out_dir, include_local=True)


if __name__ == "__main__":
    main()
