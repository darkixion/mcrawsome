"""Packaging data shared by the desktop integration and About screen."""

from pathlib import Path


def packaging_dir() -> Path:
    bundled = Path(__file__).parent / "packaging"
    return bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / "packaging"
