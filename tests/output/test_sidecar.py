import tomllib
from pathlib import Path
from typing import Any

import pytest

from mcrawsome.output import sidecar
from mcrawsome.recipe.schema import Recipe


def test_absent_measurements_are_omitted_at_every_depth(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mcraw"
    clip.write_bytes(b"input")
    measurements = {
        "suggested_crop": None,
        "motion": {"missing": None, "shake_removed": 0.1},
        "samples": [{"missing": None, "value": 2}, [3]],
        "pair": (4,),
        "stabilised": False,
    }
    path = sidecar.write_sidecar(tmp_path / "out.mp4", Recipe(), measurements, [clip])
    assert tomllib.loads(path.read_text())["measurements"] == {
        "motion": {"shake_removed": 0.1},
        "samples": [{"value": 2}, [3]],
        "pair": [4],
        "stabilised": False,
    }
    assert measurements["suggested_crop"] is None


def test_a_failed_sidecar_write_keeps_the_previous_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out.mp4"
    path = sidecar.write_sidecar(output, Recipe(), {"frames": 3}, [])
    previous = path.read_bytes()

    def broken(self: Path, data: str, *args: object, **kwargs: object) -> int:
        self.write_bytes(b"partial")
        raise OSError("sidecar disk trouble")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", broken)
        with pytest.raises(OSError, match="sidecar disk trouble"):
            sidecar.write_sidecar(output, Recipe(), {"frames": 4}, [])
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_none_inside_a_list_is_an_error_not_a_shift(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="None"):
        sidecar.write_sidecar(tmp_path / "out.mp4", Recipe(), {"crop": [1, None, 3]}, [])


def test_quick_hash_reads_an_unchanged_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    clip = tmp_path / "clip.mcraw"
    clip.write_bytes(b"a" * 1000)
    first = sidecar.quick_hash(clip)
    opened: list[Path] = []
    real_open = Path.open

    def counting(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting)
    assert sidecar.quick_hash(clip) == first
    assert opened == []  # remembered: nothing read
    clip.write_bytes(b"b" * 1000)  # the same size, rewritten in place
    os.utime(clip, ns=(10**18, 10**18))
    opened.clear()
    assert sidecar.quick_hash(clip) != first
    assert opened == [clip]
