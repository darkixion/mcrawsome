"""Direct Log videos in any registered gamut and transfer (Milestone 7 part B, task 2)."""

import dataclasses
import shutil
from pathlib import Path

import pytest

from mcrawsome.errors import McError
from mcrawsome.io.describe import encoding_name
from mcrawsome.output.export import clip_job, input_encoding_for
from mcrawsome.recipe.load import build_recipe


@pytest.mark.parametrize(
    ("develop", "named", "expected"),
    [
        ({}, "davinci", "davinci"),
        ({}, "s-gamut3-cine/s-log3", "s-gamut3-cine/s-log3"),
        ({}, None, None),
        ({"input_encoding": "acescct"}, "davinci", "acescct"),
        ({"input_gamut": "rec2020", "input_transfer": "s-log3"}, None, "rec2020/s-log3"),
        ({"input_transfer": "v-log"}, "s-gamut3-cine/s-log3", "s-gamut3-cine/v-log"),
        ({"input_gamut": "v-gamut"}, "davinci", "v-gamut/davinci-intermediate"),
        ({"input_gamut": "davinci-wg"}, "bt709", "davinci-wg/bt709"),
        ({"input_gamut": "rec709"}, "bt709", "bt709"),
        ({"input_transfer": "s-log3"}, None, None),
    ],
)
def test_input_colour_comes_from_the_fields_then_embedded_metadata(
    develop: dict[str, str], named: str | None, expected: str | None
) -> None:
    assert input_encoding_for(build_recipe([{"develop": develop}]), named) == expected


def test_encoding_names_for_info() -> None:
    assert encoding_name("davinci") == "DaVinci Wide Gamut / DaVinci Intermediate"
    assert encoding_name("s-gamut3-cine/s-log3") == "Sony S-Gamut3 (Cinema) / Sony S-Log3"


def _named(video: Path, name: str) -> Path:
    path = video.with_name(name)
    shutil.copyfile(video, path)
    return path


def test_an_unknown_video_asks_for_the_pair(
    synthetic_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holiday = _named(synthetic_video, "holiday.mp4")
    from mcrawsome.io.directlog import probe_direct_log
    from mcrawsome.output.ffmpeg import find_ffmpeg

    unknown = dataclasses.replace(probe_direct_log(find_ffmpeg()[1], holiday), encoding=None)
    monkeypatch.setattr("mcrawsome.output.export.probe_direct_log", lambda *_: unknown)
    with pytest.raises(McError, match=r"develop\.input_gamut and --develop\.input_transfer") as caught:
        clip_job(holiday, build_recipe([]))
    assert caught.value.code == "input-unreadable"
    job, _, _ = clip_job(
        holiday, build_recipe([{"develop": {"input_gamut": "rec2020", "input_transfer": "f-log"}}])
    )
    assert job.input_encoding == "rec2020/f-log"
