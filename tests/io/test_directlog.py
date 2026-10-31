import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.io.directlog import DirectLogError, encoding_from_tags, probe_direct_log
from mcrawsome.output.ffmpeg import find_ffmpeg


@pytest.mark.parametrize(
    ("primaries", "transfer", "expected"),
    [
        ("smpte432", "linear", "display-p3/linear"),
        ("bt2020", "arib-std-b67", "rec2020/hlg"),
        ("bt2020", "smpte2084", "rec2020/pq"),
        ("unknown", "bt709", None),
        ("bt709", "unknown", None),
        ("bt709", "gamma22", None),
    ],
)
def test_standard_colour_metadata_is_independent_of_the_ycbcr_matrix(
    primaries: str,
    transfer: str,
    expected: str | None,
) -> None:
    for matrix in ("bt709", "bt2020nc"):
        assert (
            encoding_from_tags({"primaries": primaries, "transfer": transfer, "matrix": matrix}) == expected
        )


@pytest.mark.parametrize(
    ("producer", "expected"), [("MotionCam", None), ("GoPro", "rec709/srgb"), ("", None)]
)
def test_generic_motioncam_tags_do_not_select_a_recording_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    producer: str,
    expected: str | None,
) -> None:
    data = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "av1",
                "r_frame_rate": "24/1",
                "width": 320,
                "height": 180,
                "nb_frames": "3",
                "color_primaries": "bt709",
                "color_transfer": "iec61966-2-1",
                "color_space": "bt709",
            },
            {"codec_type": "data", "codec_tag_string": "gpmd"},
        ],
        "format": {"duration": "0.125"},
    }
    monkeypatch.setattr(
        "mcrawsome.io.directlog.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(data)),
    )
    monkeypatch.setattr("mcrawsome.io.directlog.find_ffmpeg", lambda **kw: (Path("ffmpeg"), Path("ffprobe")))
    monkeypatch.setattr("mcrawsome.io.directlog.extract_gpmf", lambda *a: b"telemetry")
    monkeypatch.setattr(
        "mcrawsome.io.directlog.read_gpmf", lambda *a: SimpleNamespace(device={"DVNM": producer})
    )
    for name, named in (
        ("arbitrary.mp4", None),
        ("x_Davinci_Davinci-WG.mp4", "davinci"),
        ("x_Linear_Display-P3.mp4", "display-p3/linear"),
    ):
        info = probe_direct_log(Path("ffprobe"), tmp_path / name)
        assert info.encoding == (named or expected)
        assert info.encoding_source == (
            "File name (inferred)" if named else "Container tags" if expected else "Not identified"
        )
        assert bool(info.colour_note) == (not named and expected is None)


def test_dng_from_a_video_is_refused_at_planning(tmp_path: Path) -> None:
    video = tmp_path / "x_Davinci_Davinci-WG.mp4"
    video.write_bytes(b"x")
    result = CliRunner().invoke(cli, ["export", str(video), "--to", "dng", "--dry-run"])
    assert result.exit_code != 0
    assert "RAW" in result.output


@pytest.fixture
def variable_video(tmp_path: Path) -> Path:
    video = tmp_path / "vfr_Davinci_Davinci-WG.mkv"
    ffmpeg = find_ffmpeg()[0]
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
         "testsrc2=s=160x90:r=24,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.25", "-frames:v", "24",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-vf", "setpts='if(lt(N,12),N/24/TB,(N+6)/24/TB)'", "-fps_mode", "vfr", "-c:v", "ffv1", str(video)],
        check=True,
    )  # fmt: skip
    return video


def test_variable_frame_rate_uses_recorded_times_without_preconversion(variable_video: Path) -> None:
    from fractions import Fraction

    from mcrawsome.io.video_timing import presentation_times
    from mcrawsome.output.export import read_video_facts
    from mcrawsome.recipe.load import build_recipe

    ffmpeg, ffprobe = find_ffmpeg()
    info = probe_direct_log(ffprobe, variable_video)
    assert info.variable_frame_rate
    assert info.frame_count == 24
    times = presentation_times(ffprobe, variable_video)
    assert times[12] == 750_000_000
    facts, _, _ = read_video_facts(ffmpeg, variable_video, build_recipe([]), audio=False)
    assert facts.rate == Fraction(24)
    assert facts.frame_count == 30
    assert facts.tempo == 1
    assert facts.sources == (*range(12), *(11 for _ in range(6)), *range(12, 24))


def test_constant_log_can_be_converted_to_an_explicit_rate(synthetic_video: Path) -> None:
    from mcrawsome.output.export import read_video_facts
    from mcrawsome.recipe.load import build_recipe

    facts, info, _ = read_video_facts(
        find_ffmpeg()[0],
        synthetic_video,
        build_recipe([{"develop": {"input_encoding": "bt709"}, "output": {"frame_rate": 12}}]),
        audio=False,
    )
    assert not info.variable_frame_rate
    assert facts.rate == 12
    assert facts.frame_count == round(info.frame_count / float(info.fps) * 12)
    assert facts.tempo == 1


def test_vfr_preview_seeks_the_mapped_recorded_picture(
    variable_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.output.export import source_job
    from mcrawsome.pipeline.render import video_linear
    from mcrawsome.pipeline.shared import VideoDecoder
    from mcrawsome.preview.session import PreviewSession
    from mcrawsome.recipe.load import build_recipe

    recipe = build_recipe([{"motion": {"stabilise": "off"}}])
    monkeypatch.setenv("MCRAWSOME_CACHE_DIR", str(tmp_path / "cache"))
    with PreviewSession(variable_video, recipe) as session:
        images = []
        assert session._video is not None
        info = session._video[0]
        with VideoDecoder(find_ffmpeg()[0], variable_video, (info.width, info.height)) as decoder:
            for _ in range(info.frame_count):
                image = np.empty((info.height, info.width, 3), np.uint16)
                assert decoder.read_into(image)
                images.append(image)
        job = source_job(variable_video, recipe, session._facts, session._video)
        for output, source in [(11, 11), (12, 11), (16, 11), (18, 12), (29, 23)]:
            np.testing.assert_allclose(session._develop(output), video_linear(job, images[source]), atol=1e-6)


@pytest.mark.parametrize("signal_range", ["pc", "tv"])
def test_decoder_respects_full_and_video_range_at_ten_bits(tmp_path: Path, signal_range: str) -> None:
    from mcrawsome.pipeline.shared import VideoDecoder

    path = tmp_path / f"range-{signal_range}.mkv"
    y = np.tile(np.asarray([0, 64, 512, 940, 1023], dtype="<u2"), (4, 1))
    uv = np.full_like(y, 512)
    ffmpeg = find_ffmpeg()[0]
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv444p10le",
             "-s", "5x4", "-r", "24", "-color_range", signal_range, "-i", "-", "-c:v", "ffv1",
             "-color_range", signal_range, str(path)],
        input=y.tobytes() + uv.tobytes() * 2, check=True,
    )  # fmt: skip
    rgb = np.empty((4, 5, 3), np.uint16)
    with VideoDecoder(ffmpeg, path, (5, 4)) as decoder:
        assert decoder.read_into(rgb)
    expected = np.asarray([0, 64, 512, 940, 1023], dtype=float)
    expected = expected / 1023 if signal_range == "pc" else np.clip((expected - 64) / 876, 0, 1)
    np.testing.assert_allclose(rgb[1, :, 0] / 65535, expected, atol=0.005)


def test_average_rate_does_not_override_regular_pts(
    synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffprobe = find_ffmpeg()[1]
    data = json.loads(
        subprocess.check_output(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(synthetic_video),
            ],
            text=True,
        )
    )
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    video["avg_frame_rate"] = "25/1"
    video["duration"] = str((int(video["nb_frames"]) - 1) / 24)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(data))
    )
    info = probe_direct_log(ffprobe, synthetic_video)
    assert not info.variable_frame_rate
    assert info.frame_count == int(video["nb_frames"])


@pytest.fixture
def short_davinci(direct_log_davinci: Path, tmp_path: Path) -> Path:
    cut = tmp_path / "cut_Davinci_Davinci-WG.mp4"
    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-i", str(direct_log_davinci), "-map", "0", "-c", "copy",
         "-t", "2", str(cut)],
        check=True,
    )  # fmt: skip
    return cut


@pytest.mark.local
def test_info_on_direct_log(direct_log_davinci: Path) -> None:
    result = CliRunner().invoke(cli, ["info", str(direct_log_davinci)])
    assert result.exit_code == 0, result.output
    assert "Colour space" in result.output
    assert "Unknown" in result.output
    assert "pixel 10 pro xl" in result.output.lower()
    assert "Telemetry" in result.output


def test_quarter_turn_info(rotated_video: tuple[Path, int]) -> None:
    video, degrees = rotated_video
    info = probe_direct_log(find_ffmpeg()[1], video)
    assert (info.width, info.height) == (160, 96)
    assert info.rotation == -degrees % 360
    assert info.display_size == ((96, 160) if degrees % 180 else (160, 96))


def test_only_quarter_turns_are_supported(tmp_path: Path, synthetic_video: Path) -> None:
    video = tmp_path / "diagonal.mp4"
    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-display_rotation", "45", "-i", str(synthetic_video),
         "-c", "copy", str(video)], check=True,
    )  # fmt: skip
    with pytest.raises(DirectLogError, match="only quarter turns are supported"):
        probe_direct_log(find_ffmpeg()[1], video)


@pytest.mark.parametrize(("tag", "rotation"), [("90", 90), ("-90", 270), ("450", 90), ("360", 0)])
def test_legacy_rotate_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str, rotation: int) -> None:
    data = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 160,
                "height": 96,
                "r_frame_rate": "24/1",
                "tags": {"rotate": tag},
            }
        ],
        "format": {},
    }
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(data))
    )
    info = probe_direct_log(Path("ffprobe"), tmp_path / "video.mp4")
    assert info.rotation == rotation


def test_display_matrix_takes_precedence_over_rotate_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 160,
                "height": 96,
                "r_frame_rate": "24/1",
                "tags": {"rotate": "180"},
                "side_data_list": [{"rotation": -90}],
            }
        ],
        "format": {},
    }
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(data))
    )
    assert probe_direct_log(Path("ffprobe"), tmp_path / "video.mp4").rotation == 90


def test_measurements_keep_recorded_orientation(
    rotated_video: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.analysis import measure
    from mcrawsome.output.export import clip_job
    from mcrawsome.pipeline.render import iter_frames
    from mcrawsome.recipe.load import build_recipe

    video, _ = rotated_video
    job, count, _ = clip_job(video, build_recipe([]))
    seen: list[tuple[int, int, int]] = []

    def spy(probe: Any, *args: Any, **kwargs: Any) -> Any:
        frames = iter_frames(probe, *args, **kwargs)
        seen.append((probe.rotation, *probe.size))
        return frames

    monkeypatch.setattr(measure, "iter_frames", spy)
    stats = measure.measure_samples(dataclasses.replace(job, rotation=90), count, workers=2)
    assert stats.frames == 3
    assert seen == [(0, 160, 96)]


def test_indexed_movie_probe_does_not_scan_compressed_packets(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    data = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "24/1",
                "nb_frames": "240",
                "duration": "10",
            }
        ],
        "format": {},
    }

    def probe(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(data))

    monkeypatch.setattr(subprocess, "run", probe)
    assert probe_direct_log(Path("ffprobe"), Path("clip.mp4")).frame_count == 240
    assert len(calls) == 1
    assert "-count_packets" not in calls[0]


@pytest.mark.parametrize(
    "curve",
    [
        "S-Log3",
        "Apple Log",
        "Canon Log 3",
        "V-Log",
        "LogC4",
        "F-Log2",
        "N-Log",
        "D-Log M",
        "DaVinci Intermediate",
    ],
)
@pytest.mark.parametrize("scope", ["stream", "container"])
def test_log_import_recognises_explicit_recording_metadata(curve: str, scope: str) -> None:
    from mcrawsome.io.directlog import embedded_log_profile

    video: dict[str, Any] = {"color_primaries": "bt709", "color_transfer": "bt709"}
    container: dict[str, Any] = {}
    (video if scope == "stream" else container)["tags"] = {"transfer_function": curve}
    assert embedded_log_profile(video, container) is not None


@pytest.mark.parametrize(
    "transfer", ["bt709", "iec61966-2-1", "linear", "smpte2084", "arib-std-b67", "unknown"]
)
def test_sdr_hdr_and_unknown_are_not_log_even_with_wide_gamut_or_high_bit_depth(transfer: str) -> None:
    from mcrawsome.io.directlog import embedded_log_profile

    video = {
        "color_primaries": "bt2020",
        "color_transfer": transfer,
        "pix_fmt": "yuv422p12le",
        "codec_name": "prores",
        "profile": "HQ",
    }
    assert embedded_log_profile(video, {"tags": {"title": "Apple Log", "comment": "S-Log3"}}) is None


def test_conflicting_log_profile_metadata_is_not_guessed() -> None:
    from mcrawsome.io.directlog import embedded_log_profile

    assert (
        embedded_log_profile({"tags": {"gamma": "S-Log3"}}, {"tags": {"transfer_function": "Apple Log"}})
        is None
    )
    assert embedded_log_profile({"tags": {"gamma": "BT.709", "log_profile": "Apple Log"}}, {}) is None


@pytest.mark.parametrize("transfer", ["log", "log100", "log_sqrt", "log316"])
def test_standard_log_transfer_declaration_is_recognised(transfer: str) -> None:
    from mcrawsome.io.directlog import embedded_log_profile

    assert embedded_log_profile({"color_transfer": transfer}, {}) is not None
