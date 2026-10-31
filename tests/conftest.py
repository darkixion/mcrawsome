import json
import os
import shutil
import struct
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pytest

from mcrawsome.io.container import MOTION_SAMPLE, OIS_SAMPLE, ContainerWriter, ItemType
from mcrawsome.preview.backend import PreviewBackend

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from mcrawsome.io.metadata import ContainerMetadata, FrameMetadata

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "data"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Real GPU decodes run only when explicitly enabled. In a parallel run (pytest-xdist) the slow and
    local tests go first, so the longest ones do not finish alone at the end."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        items.sort(key=lambda item: not any(item.get_closest_marker(m) for m in ("slow", "local")))
    if os.environ.get("MCRAWSOME_GPU_TESTS") != "1":
        skip = pytest.mark.skip(reason="needs a GPU video decoder (set MCRAWSOME_GPU_TESTS=1)")
        for item in items:
            if item.get_closest_marker("gpu"):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _private_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read or write the user's cache, state or settings (encoder probes, the job queue, the
    interface's settings), and queueing starts no background runner that could outlive them."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MCRAWSOME_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MCRAWSOME_MOUNT_DIR", str(tmp_path / "mounts"))
    monkeypatch.setenv("MCRAWSOME_NO_AUTOSTART", "1")
    monkeypatch.setattr("mcrawsome.processes.systemd_available", lambda: False)
    monkeypatch.setattr("mcrawsome.processes.requires_systemd", lambda: False)


def _fixture(name: str) -> Path:
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"{path} missing: run scripts/make_fixtures.py with the approved source clips")
    return path


@pytest.fixture
def fixture_12bit() -> Path:
    return _fixture("clip_12bit.mcraw")


@pytest.fixture
def fixture_10bit_rotated() -> Path:
    return _fixture("clip_10bit_rotated.mcraw")


LOCAL_DIR = Path(os.environ.get("MCRAWSOME_LOCAL_SAMPLES", Path.home() / "Downloads" / "motioncam"))


def _local(name: str) -> Path:
    """The owner's local-only samples: never committed; tests using them skip elsewhere."""
    path = LOCAL_DIR / name
    if not path.exists():
        pytest.skip(f"local sample {name} not present (set MCRAWSOME_LOCAL_SAMPLES)")
    return path


SYNTHETIC_STEP = 41_666_667  # ns between frames at 24 fps
SYNTHETIC_SIZE = (128, 64)
_DATA = Path(__file__).parent / "data"


def _table(values: list[int]) -> bytes:
    padded = values + [0] * (-len(values) % 64)
    return struct.pack("<I", len(padded)) + b"".join(
        b"\xf0\x00" + struct.pack("<64H", *padded[i : i + 64]) for i in range(0, len(padded), 64)
    )


def _payload(level: int, size: tuple[int, int] = SYNTHETIC_SIZE) -> bytes:
    """A frame of flat 4x64 blocks (no residuals): each block's four channels are its references."""
    width, height = size
    refs: list[int] = []
    for block in range(width // 64 * height // 4):
        refs += [level + 7 * (block % 5), level + 3, level + 3, level + 11 * (block % 3)]
    bits, references = _table([0] * len(refs)), _table(refs)
    return struct.pack("<4I", width, height, 16, 16 + len(bits)) + bits + references


def write_mcraw(
    path: Path,
    timestamps: Sequence[int],
    neutrals: Sequence[tuple[float, float, float]] | None = None,
    audio: npt.NDArray[np.int16] | None = None,
    size: tuple[int, int] = SYNTHETIC_SIZE,
    *,
    container: dict[str, Any] | None = None,
    frame_fields: dict[str, Any] | None = None,
    motion: Sequence[tuple[ItemType, npt.NDArray[np.void]]] = (),
) -> Path:
    """A small decodable clip (SYNTHETIC_SIZE) with these frame timestamps and white balances, and
    `audio` (samples x 2 at 48 kHz, from the first frame; silence by default). `container` and
    `frame_fields` add to the recorded metadata; `motion` chunks are recorded after the audio."""
    meta = json.loads((_DATA / "container_metadata_174122.json").read_text()) | (container or {})
    frame = json.loads((_DATA / "frame_metadata_174122.json").read_text())
    width, height = size
    frame |= {"width": width, "height": height, "originalWidth": width, "originalHeight": height}
    frame |= {"rowStride": width * 2, "orientation": 0} | (frame_fields or {})
    if audio is None:
        span = (timestamps[-1] - timestamps[0]) / 1e9 + 0.1
        audio = np.zeros((round(span * 48000), 2), np.int16)
    with ContainerWriter(path, meta) as writer:
        for i, ts in enumerate(timestamps):
            neutral = {"asShotNeutral": list(neutrals[i])} if neutrals is not None else {}
            metadata = json.dumps(frame | {"timestamp": ts} | neutral).encode()
            writer.add_frame(ts, _payload(100 + 40 * i, size), metadata)
        writer.add_audio(audio.astype("<i2").tobytes(), timestamps[0])
        for item_type, samples in motion:
            writer.add_motion(item_type, samples)
    return path


# A back camera at 90 degrees whose 128x64 frames are the centre of a 160x96 array, unbinned: a 100 px
# focal length, the optical centre 4 px right of the frame's, 8 ms readout over the array's 96 rows.
RECORDED_CONTAINER = {
    "lensFacing": 1,
    "sensorOrientation": 90,
    "lensIntrinsicCalibration": [100.0, 100.0, 84.0, 48.0, 0.0],
    "lensDistortion": [0.05, -0.1, 0.07, 0.0, 0.0],
}
RECORDED_FRAME = {
    "originalWidth": 160,
    "originalHeight": 96,
    "isBinned": False,
    "lensIntrinsicCalibration": [100.0, 100.0, 84.0, 48.0, 0.0],
    "lensDistortion": [0.05, -0.1, 0.07, 0.0, 0.0],
    "rollingShutterSkewNs": "8000000",
}


def recorded_motion(
    first_ns: int, seconds: float, *, shake_deg_s: float = 5.0, ois_px: float = 0.0, chunk_s: float = 0.1
) -> list[tuple[ItemType, npt.NDArray[np.void]]]:
    """Motion as MotionCam 5.1 records it, from half a second before `first_ns` to half a second after
    `seconds`: 400 Hz gyro with a 3 Hz shake about the device's X axis (which moves the picture side to side
    on a back camera at 90 degrees), the accelerometer reading gravity along X, and 200 Hz OIS lens shifts
    with an `ois_px` amplitude along the picture's x, in chunks of `chunk_s`."""
    chunks: list[tuple[ItemType, npt.NDArray[np.void]]] = []
    start, end = first_ns - 500_000_000, first_ns + round((seconds + 0.5) * 1e9)
    for rate, item_type in (
        (400, ItemType.GYRO_DATA),
        (400, ItemType.ACCELEROMETER_DATA),
        (200, ItemType.OIS_DATA),
    ):
        times = np.arange(start, end, round(1e9 / rate), dtype=np.int64)
        phase = 2 * np.pi * 3 * (times - first_ns) / 1e9
        samples = np.zeros(len(times), dtype=OIS_SAMPLE if item_type == ItemType.OIS_DATA else MOTION_SAMPLE)
        samples["t"] = times
        if item_type == ItemType.GYRO_DATA:
            samples["x"] = np.radians(shake_deg_s) * np.sin(phase)
        elif item_type == ItemType.ACCELEROMETER_DATA:
            samples["x"] = 9.80665
        else:
            samples["x"] = ois_px * np.sin(phase)
        per = max(1, round(chunk_s * rate))
        chunks += [(item_type, samples[i : i + per]) for i in range(0, len(samples), per)]
    return chunks


@pytest.fixture
def tiny_raw(tmp_path: Path) -> Path:
    """Three tiny decodable RAW frames for wrapper and recorded-setting assertions."""
    return write_mcraw(tmp_path / "tiny.mcraw", [i * SYNTHETIC_STEP for i in range(3)])


@pytest.fixture
def white_balance_raw(tmp_path: Path) -> Path:
    """Three 128x64 flat frames with a midtone neutral near 5000 K, tint 0."""
    meta = json.loads((_DATA / "container_metadata_174122.json").read_text())
    frame = json.loads((_DATA / "frame_metadata_174122.json").read_text())
    frame |= {
        "width": 128,
        "height": 64,
        "originalWidth": 128,
        "originalHeight": 64,
        "rowStride": 256,
        "orientation": 0,
    }
    refs = [364, 640, 640, 418] * 32
    bits, references = _table([0] * len(refs)), _table(refs)
    payload = struct.pack("<4I", 128, 64, 16, 16 + len(bits)) + bits + references
    path = tmp_path / "neutral.mcraw"
    with ContainerWriter(path, meta) as writer:
        for i in range(3):
            stamp = i * SYNTHETIC_STEP
            writer.add_frame(stamp, payload, json.dumps(frame | {"timestamp": stamp}).encode())
    return path


@pytest.fixture
def make_mcraw() -> Callable[..., Path]:
    return write_mcraw


# The synthetic frames' receive time (frame_metadata_174122.json): the link puts the last frame there.
SYNTHETIC_RECEIVED_MS = 1791132083080


def write_gcsv(
    path: Path,
    frames: int,
    *,
    shake_deg_s: float = 5.0,
    jolt_at: float | None = None,
    vertical_m_s2: float = 0.0,
) -> Path:
    """A 400 Hz Gyroflow log covering a synthetic clip of `frames` (from write_mcraw, 24 fps) with two
    seconds either side: 3 Hz shake, an optional jolt `jolt_at` seconds after the first frame and
    optional 1.8 Hz up-and-down movement."""
    first_ms = SYNTHETIC_RECEIVED_MS - (frames - 1) * SYNTHETIC_STEP / 1e6
    seconds = frames / 24 + 4
    t = np.arange(0, seconds, 1 / 400)
    gyro = np.zeros((len(t), 3))
    gyro[:, 0] = np.radians(shake_deg_s) * np.sin(2 * np.pi * 3 * t)
    if jolt_at is not None:
        gyro[:, 2] = np.radians(90) * np.exp(-(((t - 2 - jolt_at) / 0.08) ** 2))
    up = 9.80665 + vertical_m_s2 * np.sin(2 * np.pi * 1.8 * t)
    rows = "".join(
        f"{round(time * 1000)},{','.join(str(round(v * 1e6)) for v in g)},0,{round(a * 1e6)},0\n"
        for time, g, a in zip(t, gyro, up, strict=True)
    )
    path.write_text(
        f"GYROFLOW IMU LOG\ntimestamp,{first_ms - 2000}\ntscale,0.001\ngscale,0.000001\nascale,0.000001\n"
        f"orientation,XYZ\nt,gx,gy,gz,ax,ay,az\n{rows}"
    )
    return path


@pytest.fixture
def make_gcsv() -> Callable[..., Path]:
    return write_gcsv


@pytest.fixture
def local_dir() -> Path:
    return LOCAL_DIR


@pytest.fixture
def direct_log_davinci() -> Path:
    return _local("261011_095029_VIDEO_25mm_Davinci_Davinci-WG.mp4")


@pytest.fixture
def direct_log_bt709() -> Path:
    return _local("261011_094111_VIDEO_25mm_BT.709_BT709.mp4")


def generate_synthetic_video(tmp_path: Path) -> Path:
    """30 frames of a moving test pattern with a tone, named like a DaVinci Direct Log recording."""
    from mcrawsome.output.ffmpeg import find_ffmpeg

    path = tmp_path / "synthetic_Davinci_Davinci-WG.mp4"
    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-f", "lavfi", "-i",
         "testsrc2=s=320x180:r=24,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-frames:v", "30", "-t", "1.25",
         "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-colorspace", "bt709", "-movflags", "+write_colr+use_metadata_tags",
         "-metadata", "transfer_function=DaVinci Intermediate", "-c:a", "aac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def generate_direct_log_video(tmp_path: Path, codec: str) -> Path:
    """Three frames with an off-centre white box, in each supported Direct Log codec."""
    from mcrawsome.output.ffmpeg import find_ffmpeg

    options = {
        "av1": ["-c:v", "libsvtav1", "-preset", "12", "-svtav1-params", "lp=2"],
        "prores": ["-c:v", "prores_ks", "-profile:v", "3", "-threads", "2"],
    }
    suffix = ".mov" if codec == "prores" else ".mp4"
    path = tmp_path / f"{codec}_BT.709_BT709{suffix}"
    if codec == "hevc":
        # Decoder coverage does not require a GPL encoder in the distribution build.
        shutil.copyfile(_DATA / "synthetic_hevc_10bit.mp4", path)
        return path
    subprocess.run(
        [str(find_ffmpeg()[0]), "-v", "error", "-f", "lavfi", "-i",
         "color=c=0x202020:s=160x96:r=24,drawbox=x=8:y=12:w=24:h=16:color=white:t=fill,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.125",
         "-pix_fmt", "yuv422p10le" if codec == "prores" else "yuv420p10le",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         *options[codec], "-c:a", "pcm_s16le" if codec == "prores" else "aac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def generate_rotated_video(tmp_path: Path, degrees: int) -> tuple[Path, int]:
    from mcrawsome.output.ffmpeg import find_ffmpeg

    plain, video = tmp_path / "plain.mp4", tmp_path / "rotated_BT.709_BT709.mp4"
    ffmpeg = find_ffmpeg()[0]
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
         "color=c=0x202020:s=160x96:r=24,drawbox=x=8:y=12:w=24:h=16:color=white:t=fill,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
         "-frames:v", "3", "-c:v", "mpeg4", "-q:v", "2",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-movflags", "+write_colr", str(plain)], check=True,
    )  # fmt: skip
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-display_rotation", str(degrees), "-i", str(plain),
         "-c", "copy", str(video)], check=True,
    )  # fmt: skip
    return video, degrees


@pytest.fixture(scope="session")
def generated_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Immutable shared source; tests receive a private copy they can move or edit."""
    return generate_synthetic_video(tmp_path_factory.mktemp("synthetic-video"))


@pytest.fixture
def synthetic_video(generated_video: Path, tmp_path: Path) -> Path:
    return Path(shutil.copyfile(generated_video, tmp_path / generated_video.name))


@pytest.fixture(scope="session", params=["av1", "hevc", "prores"])
def generated_direct_log_video(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Path:
    return generate_direct_log_video(tmp_path_factory.mktemp("direct-log"), str(request.param))


@pytest.fixture
def direct_log_video(generated_direct_log_video: Path, tmp_path: Path) -> Path:
    return Path(shutil.copyfile(generated_direct_log_video, tmp_path / generated_direct_log_video.name))


@pytest.fixture(scope="session", params=[0, 90, 180, 270])
def generated_rotated_video(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, int]:
    return generate_rotated_video(tmp_path_factory.mktemp("rotation"), int(request.param))


@pytest.fixture
def rotated_video(generated_rotated_video: tuple[Path, int], tmp_path: Path) -> tuple[Path, int]:
    source, degrees = generated_rotated_video
    return Path(shutil.copyfile(source, tmp_path / source.name)), degrees


@pytest.fixture(scope="session")
def prores_references(
    generated_video: Path, tmp_path_factory: pytest.TempPathFactory
) -> Callable[[int], tuple[Path, bytes, float]]:
    """Clean references with fixed input, recipe, workers and chunk policy; never resumed or edited."""
    from click.testing import CliRunner

    from mcrawsome.cli.main import cli
    from mcrawsome.output import chunks
    from mcrawsome.output.ffmpeg import find_ffmpeg
    from mcrawsome.output.sidecar import sidecar_path

    references: dict[int, tuple[Path, bytes, float]] = {}

    def get(chunk_frames: int) -> tuple[Path, bytes, float]:
        if chunk_frames not in references:
            folder = tmp_path_factory.mktemp("prores-reference")
            # Reference creation must never read or write the owner's cache or settings.
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("XDG_CONFIG_HOME", str(folder / "config"))
                patch.setenv("MCRAWSOME_CACHE_DIR", str(folder / "cache"))
                patch.setenv("MCRAWSOME_STATE_DIR", str(folder / "state"))
                patch.setattr(chunks, "CHUNK_FRAMES", chunk_frames)
                output = folder / "whole.mov"
                result = CliRunner().invoke(
                    cli,
                    [
                        "export",
                        str(generated_video),
                        "--to",
                        "prores",
                        "--look",
                        "gorgeous",
                        "--stabilise",
                        "off",
                        "--workers",
                        "2",
                        "--output",
                        str(output),
                    ],
                )
                assert result.exit_code == 0, result.output
                decoded = subprocess.run(
                    [
                        str(find_ffmpeg()[0]),
                        "-v",
                        "error",
                        "-i",
                        str(output),
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "yuv422p10le",
                        "-",
                    ],
                    capture_output=True,
                    check=True,
                ).stdout
                side = tomllib.loads(sidecar_path(output).read_text())
                assert side["measurements"]["audio_offset_s"] == 0.0
                references[chunk_frames] = output, decoded, 0.0
        return references[chunk_frames]

    return get


@pytest.fixture
def clean_prores_reference(
    prores_references: Callable[[int], tuple[Path, bytes, float]],
) -> tuple[Path, bytes, float]:
    return prores_references(240)


@pytest.fixture
def clean_chunked_prores_reference(
    prores_references: Callable[[int], tuple[Path, bytes, float]],
) -> tuple[Path, bytes, float]:
    return prores_references(10)


_FAKE = """#!{python}
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_FFMPEG_STATE"])
real = {real!r}
if args == ["-version"]:
    print(os.environ.get("FAKE_FFMPEG_VERSION", "ffmpeg version 7.1-fake"))
    print("configuration: fake")
    sys.exit(0)
if "-encoders" in args:
    names = ["libsvtav1", "libopus"] + [
        f"{{codec}}_{{name}}" for name in ("nvenc", "vaapi", "qsv") for codec in ("av1", "hevc", "h264")
    ]
    names += os.environ.get("FAKE_FFMPEG_EXTRA_ENCODERS", "").split()
    print("".join(f" V....D {{name}}  fake {{name}}\\n" for name in names))
    sys.exit(0)
if "-decoders" in args:
    print(" V....D libdav1d  dav1d")
    sys.exit(0)
codec = args[args.index("-c:v") + 1] if "-c:v" in args else ""
working = os.environ.get("FAKE_FFMPEG_WORKING", "").split(",")
if args[-1] == "-" and "null" in args:  # a trial encode
    with (state / "runs").open("a") as runs:
        runs.write(" ".join(args) + "\\n")
    if codec in working:
        sys.exit(0)
    if "qsv" in codec:
        print("[AVHWDeviceContext @ 0x1] Error creating a MFX session: -9.", file=sys.stderr)
        print("Failed to set value 'qsv=hw' for option 'init_hw_device': Unknown error", file=sys.stderr)
        print("Error parsing global options: Unknown error occurred", file=sys.stderr)
    else:
        print(f"[{{codec}} @ 0x1] Cannot load libcuda.so.1", file=sys.stderr)
    sys.exit(1)
if codec == "liboapv":  # APV exports use ProRes as a stand-in on FFmpeg without OpenAPV
    with (state / "runs").open("a") as runs:
        runs.write(" ".join(args) + "\\n")
    args[args.index("-c:v") + 1] = "prores_ks"
    index = args.index("-qp")
    args[index:index + 2] = ["-profile:v", "3"]
    os.execv(real, [real, *args])
if codec.endswith("_vaapi"):  # an export's chunk: the first succeeds, later ones fail after 5 frames
    size = args[args.index("-s") + 1]
    width, height = (int(v) for v in size.split("x"))
    frame = width * height * 6
    out = args[-1]
    if "chunk_0000" in out or os.environ.get("FAKE_FFMPEG_SUCCEED"):
        count = len(sys.stdin.buffer.read()) // frame
        subprocess.run(
            [real, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={{size}}:r=24", "-frames:v",
             str(count), "-c:v", "ffv1", out],
            check=True,
        )
        sys.exit(0)
    for _ in range(5):
        sys.stdin.buffer.read(frame)
    print(os.environ.get("FAKE_FFMPEG_FAILURE", "fake encoder failure"), file=sys.stderr)
    sys.exit(1)
os.execv(real, [real, *args])
"""


@pytest.fixture
def fake_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `ffmpeg` first on PATH: trial encodes succeed for FAKE_FFMPEG_WORKING, everything else
    goes to the real FFmpeg. Its trial runs are listed in `<state>/runs`."""
    from mcrawsome.output.ffmpeg import FfmpegError, find_ffmpeg

    try:
        pair = find_ffmpeg()
    except FfmpegError:
        pytest.skip("FFmpeg not found")
    real_ffmpeg, real_ffprobe = (str(path) for path in pair)
    folder = tmp_path / "fake-bin"
    folder.mkdir()
    fake = folder / "ffmpeg"
    fake.write_text(_FAKE.format(python=sys.executable, real=real_ffmpeg))
    fake.chmod(0o755)
    (folder / "ffprobe").symlink_to(real_ffprobe)
    state = tmp_path / "fake-state"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("MCRAWSOME_FFMPEG", str(fake))
    monkeypatch.setenv("FAKE_FFMPEG_STATE", str(state))
    monkeypatch.setenv("FAKE_FFMPEG_WORKING", "av1_vaapi,hevc_vaapi,h264_vaapi")
    monkeypatch.setenv("MCRAWSOME_VAAPI_DEVICE", "/dev/dri/renderD128")
    return fake


# The API's tests (tests/server)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """The server's only library folder."""
    folder = tmp_path / "library"
    folder.mkdir()
    return folder


@pytest.fixture
def client(library: Path) -> Iterator["TestClient"]:
    """The API at its default address, with `library` as its library and no queue runner."""
    from fastapi.testclient import TestClient

    from mcrawsome.server.app import create_app
    from mcrawsome.server.config import ServerConfig

    app = create_app(ServerConfig(roots=[library], workers=2))
    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        yield client


@pytest.fixture
def video(synthetic_video: Path, library: Path) -> Path:
    """The synthetic Direct Log video, inside the library."""
    return Path(shutil.move(synthetic_video, library / synthetic_video.name))


@pytest.fixture(params=["cpu", "opencl"])
def preview_compute(request: pytest.FixtureRequest) -> Iterator[PreviewBackend]:
    """Run the same numerical contracts on the CPU and on an explicitly enabled real GPU."""
    use_gpu = request.param == "opencl"
    if use_gpu and os.environ.get("MCRAWSOME_OPENCL_TESTS") != "1":
        pytest.skip("needs an OpenCL GPU (set MCRAWSOME_OPENCL_TESTS=1)")
    selected = PreviewBackend(enabled=use_gpu)
    if use_gpu:
        assert selected._device() is not None, selected.reason
    try:
        yield selected
        if use_gpu:
            assert selected.reason == "GPU preview active", selected.reason
    finally:
        selected.close()


STILL_BLACK, STILL_WHITE = 64, 4095


def still_frame(
    width: int, height: int, neutral: tuple[float, float, float] = (0.5, 1.0, 0.7)
) -> "FrameMetadata":
    """Frame metadata for a synthetic still: a gentle lens-shading bowl, as MotionCam records it."""
    from mcrawsome.io.metadata import FrameMetadata

    lw, lh = 9, 7
    yy, xx = np.mgrid[0:lh, 0:lw]
    bowl = 1 + 1.0 * (((xx - (lw - 1) / 2) / lw) ** 2 + ((yy - (lh - 1) / 2) / lh) ** 2)
    planes = [(bowl * (1 + 0.03 * c)).ravel().tolist() for c in range(4)]
    return FrameMetadata.model_validate(
        {
            "width": width, "height": height, "originalWidth": width, "originalHeight": height,
            "orientation": 0, "compressionType": 7, "asShotNeutral": list(neutral),
            "dynamicBlackLevel": [STILL_BLACK] * 4, "dynamicWhiteLevel": STILL_WHITE, "iso": 100.0,
            "exposureTime": 1e7, "lensShadingMap": planes, "lensShadingMapWidth": lw,
            "lensShadingMapHeight": lh, "recvdTimestampMs": 0,
            "noiseProfile": [2e-5, 1e-7, 1.5e-5, 1e-7, 1.5e-5, 1e-7, 2.5e-5, 1e-7],
        }
    )  # fmt: skip


def still_container(cfa: str = "rggb") -> "ContainerMetadata":
    from mcrawsome.io.metadata import parse_container_metadata

    meta = json.loads((_DATA / "container_metadata_pixel10.json").read_text())
    return parse_container_metadata(meta | {"sensorArrangment": cfa})


def mosaic_from_camera(camera: npt.NDArray[np.float32], cfa: str = "rggb") -> npt.NDArray[np.uint16]:
    """The mosaic a sensor records for camera-native linear RGB (1.0 = white level)."""
    h, w = camera.shape[:2]
    out = np.empty((h, w), np.uint16)
    for i, (y, x) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        channel = "rgb".index(cfa[i])
        values = STILL_BLACK + np.clip(camera[y::2, x::2, channel], 0, 1) * (STILL_WHITE - STILL_BLACK)
        out[y::2, x::2] = np.rint(values).astype(np.uint16)
    return out


def write_still(
    path: Path,
    mosaic: npt.NDArray[np.uint16],
    cfa: str = "rggb",
    neutral: tuple[float, float, float] = (0.5, 1.0, 0.7),
    orientation: int = 1,
    compression: str = "none",
    shading: bool = True,
    **info: Any,
) -> Path:
    """A synthetic MotionCam-style DNG still written with mcrawsome's own DNG writer."""
    import dataclasses
    from fractions import Fraction

    from mcrawsome.output.dng import dng_info, write_dng

    frame = still_frame(mosaic.shape[1], mosaic.shape[0], neutral)
    details = dng_info(still_container(cfa), frame, Fraction(30), exposure_stops=0.0)
    details = dataclasses.replace(
        details, orientation=orientation, make="Google", model="Pixel 10 Pro XL", **info
    )
    if not shading:
        details = dataclasses.replace(details, gain_maps=b"\0\0\0\0")
    write_dng(path, mosaic, details, compression)  # type: ignore[arg-type]
    return path


def scene(height: int, width: int, seed: int = 0) -> npt.NDArray[np.float32]:
    """A white-balanced linear test scene (0 to 0.8): a grey ramp, colour patches and a soft texture."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    grey = 0.02 + 0.6 * xx / max(width - 1, 1)
    out = np.repeat(grey[..., None], 3, axis=2)
    colours = rng.uniform(0.05, 0.8, (6, 3)).astype(np.float32)
    for i, colour in enumerate(colours):
        top, left = (i // 3) * height // 2 + height // 8, (i % 3) * width // 3 + width // 12
        out[top : top + height // 4, left : left + width // 6] = colour
    texture = 1 + 0.05 * np.sin(xx / 3.0) * np.cos(yy / 5.0)
    return np.asarray(np.clip(out * texture[..., None], 0, 0.8), np.float32)


def write_scene_still(
    path: Path,
    height: int = 120,
    width: int = 160,
    neutral: tuple[float, float, float] = (0.5, 1.0, 0.7),
    **options: Any,
) -> Path:
    """A synthetic still of `scene`, recorded through a white balance of `neutral`."""
    camera = scene(height, width) * np.asarray(neutral, np.float32)
    cfa = options.pop("cfa", "rggb")
    return write_still(path, mosaic_from_camera(camera, cfa), cfa=cfa, neutral=neutral, **options)


@pytest.fixture(params=["cpu", "opencl"])
def full_develop(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the existing full-quality colour assertions on both implementations."""
    from mcrawsome.develop.raw import Developer
    from mcrawsome.preview.opencl import OpenCL

    if request.param == "cpu":
        yield
        return
    if os.environ.get("MCRAWSOME_OPENCL_TESTS") != "1":
        pytest.skip("needs an OpenCL GPU (set MCRAWSOME_OPENCL_TESTS=1)")
    device = OpenCL()
    try:
        monkeypatch.setattr(Developer, "develop", lambda developer, raw: device.develop_full(developer, raw))
        yield
    finally:
        device.close()
