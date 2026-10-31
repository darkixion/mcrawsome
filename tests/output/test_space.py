import errno
import json
import shutil
from collections import namedtuple
from pathlib import Path

import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.errors import McError
from mcrawsome.output.space import check_space, estimate_bytes, human_size
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.schema import Recipe

_Usage = namedtuple("_Usage", "total used free")


def _recipe(**output: object) -> Recipe:
    return build_recipe([{"output": output}])


def test_cinemadng_is_the_sensor_size_and_a_third_with_lossless_jpeg() -> None:
    full = estimate_bytes(_recipe(kind="dng"), 4000, 3000, 100, 24.0)
    assert full == pytest.approx(4000 * 3000 * 2 * 100, rel=0.01)
    lossless = estimate_bytes(_recipe(kind="dng", dng_compression="lossless"), 4000, 3000, 100, 24.0)
    assert lossless == pytest.approx(full / 3, rel=0.01)


def test_openexr_is_half_float_rgb_and_grows_with_frames() -> None:
    one = estimate_bytes(_recipe(kind="exr"), 1920, 1080, 100, 24.0)
    assert one == pytest.approx(1920 * 1080 * 6 * 100, rel=0.01)
    assert estimate_bytes(_recipe(kind="exr"), 1920, 1080, 200, 24.0) == pytest.approx(2 * one, rel=0.01)


def test_dng_proxies_shrink_the_estimate_by_the_square_of_the_factor() -> None:
    def size(scale: str) -> int:
        return estimate_bytes(_recipe(kind="dng", dng_scale=scale), 3840, 2160, 240, 24.0)

    audio = int(10 * 1_536_000 / 8)
    assert size("quarter") - audio == pytest.approx((size("full") - audio) / 16, rel=1e-3)
    assert size("eighth") - audio == pytest.approx((size("full") - audio) / 64, rel=1e-3)


def test_check_space_names_both_sizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(10**12, 0, 1_100_000_000))
    check_space(tmp_path, 1_000_000_000)  # exactly 1.1x is enough
    with pytest.raises(McError) as caught:
        check_space(tmp_path / "new" / "folder", 1_050_000_000)  # a folder that does not exist yet
    assert caught.value.code == "disk-full"
    assert "1.1 GB free" in str(caught.value)
    assert "1.2 GB" in str(caught.value)
    assert human_size(1500) == "1.5 KB"


def test_an_oserror_with_enospc_is_a_full_disk() -> None:
    from mcrawsome.output.export import is_disk_full

    assert is_disk_full(OSError(errno.ENOSPC, "No space left on device"))
    assert not is_disk_full(OSError(errno.EACCES, "denied"))


# ---- batches ----


def _batch(*args: str) -> tuple[int, dict[str, object]]:
    result = CliRunner().invoke(cli, ["export", *args, "--json"])
    return result.exit_code, json.loads(result.output)


def _item(clip: Path, folder: Path, **output: object) -> tuple[Path, Recipe, Path, tuple[int, int] | None]:
    return clip, _recipe(kind="exr", **output), folder / f"{clip.stem}.out", None


def test_a_batch_that_will_not_fit_is_refused_before_anything_renders(
    synthetic_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = tmp_path / "second_Davinci_Davinci-WG.mp4"
    shutil.copy(synthetic_video, second)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(10**9, 0, 1000))
    code, document = _batch(str(synthetic_video), str(second), "--to", "exr")
    assert code == 1
    error = document["error"]
    assert isinstance(error, dict)
    assert error["code"] == "disk-full"
    assert "1.0 KB free" in error["message"]
    assert "--no-space-check" in error["message"]
    assert not list(tmp_path.glob("*_EXR"))


def test_check_batch_sums_clips_in_one_folder(
    synthetic_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.output.space import check_batch

    one = estimate_bytes(_recipe(kind="exr"), 320, 180, 30, 24.0)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(10**12, 0, int(1.1 * one * 1.5)))
    check_batch([_item(synthetic_video, tmp_path)])
    with pytest.raises(McError):
        check_batch([_item(synthetic_video, tmp_path), _item(synthetic_video, tmp_path)])
    with pytest.raises(McError):
        check_batch([_item(synthetic_video, tmp_path), _item(synthetic_video, other)])


def test_check_batch_names_every_folder_on_the_short_filesystem(
    synthetic_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.output.space import check_batch

    one = estimate_bytes(_recipe(kind="exr"), 320, 180, 30, 24.0)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    free = int(1.1 * one * 1.5)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(10**12, 0, free))
    with pytest.raises(McError) as caught:
        check_batch([_item(synthetic_video, first), _item(synthetic_video, second)])
    message = str(caught.value)
    assert caught.value.code == "disk-full"
    assert str(first) in message
    assert str(second) in message
    assert message.count(human_size(int(one * 1.1))) >= 2  # what each folder needs
    assert f"{human_size(free)} free" in message
    assert "--no-space-check" in message
