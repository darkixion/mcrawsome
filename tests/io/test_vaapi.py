import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io import vaapi
from mcrawsome.io.directlog import probe_direct_log
from mcrawsome.io.vaapi import CODECS, decode_device, probe_decoders
from mcrawsome.output.ffmpeg import FfmpegError, find_ffmpeg
from mcrawsome.pipeline.shared import VideoDecoder

FFMPEG = Path("/usr/bin/ffmpeg")


def _frames(value: int, count: int = 4) -> bytes:
    return np.full(count * 256 * 144 * 3, value, np.uint16).tobytes()


@pytest.fixture
def trials(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Fake decodes: the processor gives frames of 1000, the GPU 1000 + MCRAWSOME_FAKE_GPU_OFFSET."""
    runs: list[tuple[str, str | None]] = []

    def decode(ffmpeg: Path, sample: Path, device: str | None) -> bytes | str:
        runs.append((sample.name, device))
        if device is None:
            return _frames(1000)
        if device == "/dev/dri/broken":
            return "Failed to initialise VAAPI connection: -1 (unknown libva error)."
        return _frames(1000 + int(os.environ.get("MCRAWSOME_FAKE_GPU_OFFSET", "0")))

    monkeypatch.setattr(vaapi, "_decode", decode)
    monkeypatch.setattr(vaapi, "_cache_key", lambda ffmpeg: {"ffmpeg": str(ffmpeg), "version": "fake"})
    monkeypatch.setenv("MCRAWSOME_VAAPI_DEVICE", "/dev/dri/renderD128")
    monkeypatch.setenv("MCRAWSOME_VAAPI_DECODE", "1")
    vaapi._REMEMBERED.clear()
    return runs


def test_gpu_frames_that_match_ffmpegs_own_decoder_are_used(trials: list[tuple[str, str | None]]) -> None:
    probes = {p.codec: p for p in probe_decoders(FFMPEG)}
    assert set(probes) == set(CODECS) == {"hevc", "av1"}
    assert all((p.works, p.reason, p.device) == (True, None, "/dev/dri/renderD128") for p in probes.values())
    assert len(trials) == 4
    assert set(trials) == {
        (f"trial-{codec}.mp4", device) for codec in CODECS for device in (None, "/dev/dri/renderD128")
    }
    assert decode_device(FFMPEG, "hevc") == decode_device(FFMPEG, "av1") == "/dev/dri/renderD128"


@pytest.mark.parametrize("offset", [0, 64])
def test_a_one_code_difference_is_accepted(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    monkeypatch.setenv("MCRAWSOME_FAKE_GPU_OFFSET", str(offset))
    assert decode_device(FFMPEG, "hevc") == "/dev/dri/renderD128"


def test_gpu_frames_that_differ_decode_on_the_processor(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_FAKE_GPU_OFFSET", "65")
    probe = next(p for p in probe_decoders(FFMPEG) if p.codec == "hevc")
    assert not probe.works
    assert probe.reason == "the GPU's frames differ from FFmpeg's own decoder by up to 65 codes"
    assert decode_device(FFMPEG, "hevc") is None


def test_a_failing_device_gives_ffmpegs_reason(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_VAAPI_DEVICE", "/dev/dri/broken")
    probe = next(p for p in probe_decoders(FFMPEG) if p.codec == "av1")
    assert (probe.works, probe.device) == (False, None)
    assert probe.reason is not None
    assert "Failed to initialise VAAPI connection" in probe.reason
    assert decode_device(FFMPEG, "av1") is None


def test_without_a_render_device_nothing_is_tried(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCRAWSOME_VAAPI_DEVICE")
    monkeypatch.setattr("glob.glob", lambda pattern: [])
    reason = "no GPU video decoder (/dev/dri/renderD* or /dev/nvidia*) found"
    assert all(p.reason == reason for p in probe_decoders(FFMPEG))
    assert trials == []


def _nvidia(monkeypatch: pytest.MonkeyPatch, nodes: list[str]) -> None:
    """These NVIDIA device nodes, besides the control nodes every NVIDIA driver adds."""
    found = [*nodes, "/dev/nvidiactl", "/dev/nvidia-modeset", "/dev/nvidia-uvm"]
    monkeypatch.setattr("glob.glob", lambda pattern: found if pattern == "/dev/nvidia*" else [])


def test_an_nvidia_gpu_decodes_when_no_render_node_does(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_VAAPI_DEVICE", "/dev/dri/broken")
    _nvidia(monkeypatch, ["/dev/nvidia0", "/dev/nvidia1"])
    probes = probe_decoders(FFMPEG)
    assert all((p.works, p.device) == (True, "cuda:0") for p in probes)
    assert decode_device(FFMPEG, "hevc") == decode_device(FFMPEG, "av1") == "cuda:0"
    assert {device for _, device in trials} == {None, "/dev/dri/broken", "cuda:0"}


def test_a_failing_nvidia_gpu_is_named_in_the_reason(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_VAAPI_DEVICE", "/dev/dri/broken")
    monkeypatch.setenv("MCRAWSOME_FAKE_GPU_OFFSET", "65")  # the NVIDIA GPU's frames differ
    _nvidia(monkeypatch, ["/dev/nvidia0"])
    probe = next(p for p in probe_decoders(FFMPEG) if p.codec == "hevc")
    assert not probe.works
    assert probe.reason is not None
    assert probe.reason.startswith("/dev/dri/broken: Failed to initialise VAAPI connection")
    assert probe.reason.endswith(
        "; NVIDIA GPU 0: the GPU's frames differ from FFmpeg's own decoder by up to 65 codes"
    )


def test_vaapi_is_kept_when_it_works_beside_an_nvidia_gpu(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _nvidia(monkeypatch, ["/dev/nvidia0"])
    assert decode_device(FFMPEG, "hevc") == "/dev/dri/renderD128"
    assert "cuda:0" not in {device for _, device in trials}


@pytest.mark.parametrize(
    ("device", "hardware"),
    [
        ("/dev/dri/renderD128", ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
                                 "-hwaccel_output_format", "vaapi"]),
        ("cuda:1", ["-hwaccel", "cuda", "-hwaccel_device", "1", "-hwaccel_output_format", "cuda"]),
    ],
)  # fmt: skip
def test_a_trial_keeps_frames_on_the_gpu_until_downloaded(
    monkeypatch: pytest.MonkeyPatch, device: str, hardware: list[str]
) -> None:
    """A decode FFmpeg quietly did itself then fails the download, so it can never pass as the GPU's."""
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"frames", stderr=b"")

    monkeypatch.setattr("mcrawsome.io.vaapi.subprocess.run", run)
    assert vaapi._decode(FFMPEG, vaapi._sample("av1"), device) == b"frames"
    [command] = commands
    assert command[command.index("-hwaccel") : command.index("-hwaccel") + 6] == hardware
    assert command[command.index("-c:v") + 1] == "av1"  # FFmpeg's own AV1 decoder decodes on the GPU
    assert command[command.index("-vf") + 1].startswith("hwdownload,format=p010le,")


@pytest.mark.parametrize("codec", ["h264", "prores", "vp9", None])
def test_other_codecs_decode_on_the_processor_without_a_trial(
    trials: list[tuple[str, str | None]], codec: str | None
) -> None:
    assert decode_device(FFMPEG, codec) is None
    assert trials == []


@pytest.mark.parametrize("setting", [None, "0", "yes"])
def test_videos_decode_on_the_processor_unless_switched_on(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> None:
    """Measured here, GPU decoding made exports 3 to 5% and preview seeks 170 to 370 ms slower."""
    if setting is None:
        monkeypatch.delenv("MCRAWSOME_VAAPI_DECODE")
    else:
        monkeypatch.setenv("MCRAWSOME_VAAPI_DECODE", setting)
    assert decode_device(FFMPEG, "hevc") is None
    assert trials == []


def test_trials_are_cached_until_refreshed(trials: list[tuple[str, str | None]]) -> None:
    first = probe_decoders(FFMPEG)
    assert len(trials) == 4
    vaapi._REMEMBERED.clear()
    assert probe_decoders(FFMPEG) == first
    assert len(trials) == 4
    cache = json.loads((Path(os.environ["MCRAWSOME_CACHE_DIR"]) / "decoders.json").read_text())
    assert cache["entries"][0]["key"]["ffmpeg"] == str(FFMPEG)
    probe_decoders(FFMPEG, refresh=True)
    assert len(trials) == 8


def test_a_damaged_cache_is_tried_again(trials: list[tuple[str, str | None]]) -> None:
    cache = Path(os.environ["MCRAWSOME_CACHE_DIR"])
    cache.mkdir(parents=True)
    (cache / "decoders.json").write_text("{not json")
    assert all(p.works for p in probe_decoders(FFMPEG))
    assert len(trials) == 4


def test_trial_clips_are_short_10_bit_hevc_and_av1() -> None:
    try:
        ffprobe = find_ffmpeg()[1]
    except FfmpegError:
        pytest.skip("FFmpeg not found")
    for codec in CODECS:
        sample = Path(vaapi.__file__).parent / "data" / f"trial-{codec}.mp4"
        assert sample.stat().st_size < 16_000
        stream = json.loads(
            subprocess.run(
                [str(ffprobe), "-v", "error", "-show_streams", "-of", "json", str(sample)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )["streams"][0]
        assert (stream["codec_name"], stream["pix_fmt"], stream["nb_frames"]) == (codec, "yuv420p10le", "4")


@pytest.mark.gpu
@pytest.mark.parametrize("codec", CODECS)
def test_the_gpu_decodes_the_trial_clips_like_ffmpeg(codec: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCRAWSOME_VAAPI_DECODE", "1")
    ffmpeg = find_ffmpeg()[0]
    vaapi._REMEMBERED.clear()
    probe = next(p for p in probe_decoders(ffmpeg, refresh=True) if p.codec == codec)
    assert probe.works, probe.reason
    assert decode_device(ffmpeg, codec) == probe.device


@pytest.mark.gpu
def test_direct_log_videos_decode_on_the_gpu_like_on_the_processor(
    direct_log_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCRAWSOME_VAAPI_DECODE", "1")
    ffmpeg, ffprobe = find_ffmpeg()
    info = probe_direct_log(ffprobe, direct_log_video)
    device = decode_device(ffmpeg, info.codec)
    assert (device is None) == (info.codec == "prores")
    size, shape = (info.width, info.height), (info.height, info.width, 3)
    decoded = []
    for where in (device, None):
        frames = []
        with VideoDecoder(ffmpeg, direct_log_video, size, device=where, codec=info.codec) as decoder:
            frame = np.empty(shape, np.uint16)
            while decoder.read_into(frame):
                frames.append(frame.astype(np.int32))
        decoded.append(np.stack(frames))
    assert len(decoded[0]) == len(decoded[1]) == 3
    assert np.abs(decoded[0] - decoded[1]).max() <= vaapi.TOLERANCE


def test_preview_uses_a_verified_gpu_by_default_and_keeps_explicit_opt_out(
    trials: list[tuple[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCRAWSOME_VAAPI_DECODE")
    assert decode_device(FFMPEG, "av1", preview=True) == "/dev/dri/renderD128"
    assert decode_device(FFMPEG, "av1") is None
    monkeypatch.setenv("MCRAWSOME_VAAPI_DECODE", "0")
    assert decode_device(FFMPEG, "av1", preview=True) is None
