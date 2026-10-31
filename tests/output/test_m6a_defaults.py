"""Milestone 6 part A adds settings to the raw and DNG paths; with their defaults the output must not change.

The references were recorded before any of those settings existed: the DNG files' SHA-256 (with the
Software tag's version blanked), from a synthetic clip. They were recorded again for the rename to
MCRAWsome, after checking that the exports differed from the previous name's only in the Software tag.
"""

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from mcrawsome import __version__
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe

STEP = 41_666_667
TIMES = [i * STEP for i in (0, 1, 2, 4, 5)]  # frame 3 was dropped, so a repeat is written

DNG_DIGESTS = {
    ("full", "lossless"): "e814bb41b57d4befcc8a1cee292574c9bc166d2eefd2763dd69dac2db0c09f10",
    ("full", "none"): "fe65d026e61cd56c2f8c2f7fe341f40278e1fd2ff263b066b29f667145fdd93c",
    ("half", "none"): "be1f3c8ebb1c7c442d03c6a15b366330806fece1f84e28ca2ce8c0e2b61245a4",
}


def _digest(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.glob("*.dng")):
        # Keep the reference independent of the newly requested frame-name separator.
        stem, number = path.name.rsplit("-", 1)
        digest.update(f"{stem}_{number}".encode())
        digest.update(path.read_bytes().replace(f"MCRAWsome {__version__}".encode(), b"MCRAWsome"))
    return digest.hexdigest()


@pytest.mark.parametrize(("scale", "compression"), sorted(DNG_DIGESTS))
def test_default_dngs_are_unchanged(
    scale: str, compression: str, tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", TIMES)
    recipe = build_recipe([{"output": {"kind": "dng", "dng_scale": scale, "dng_compression": compression}}])
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    assert result.frames == 6
    assert _digest(result.output) == DNG_DIGESTS[(scale, compression)]
