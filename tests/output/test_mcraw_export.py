import json
import os
import shutil
import struct
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerWriter, read_audio_chunk, read_frame_record, read_index
from mcrawsome.io.gcsv import read_gcsv
from mcrawsome.output.chunks import WorkFolder
from mcrawsome.output.export import ExportResult
from mcrawsome.output.sidecar import sidecar_path
from mcrawsome.recipe.load import build_recipe


@pytest.fixture
def source(tmp_path: Path) -> Path:
    data = Path(__file__).parents[1] / "data"
    metadata = json.loads((data / "container_metadata_174122.json").read_text())
    metadata["extraData"] |= {"audioSampleRate": 100, "audioChannels": 1}
    frame = json.loads((data / "frame_metadata_174122.json").read_text())
    path = tmp_path / "source.mcraw"
    with ContainerWriter(path, metadata) as writer:
        for i in range(4):
            timestamp = (5 + i) * 1_000_000_000
            meta = frame | {"timestamp": str(timestamp), "recvdTimestampMs": 100_000 + i * 1000}
            writer.add_frame(timestamp, bytes([i]) * 20, json.dumps(meta).encode())
        for timestamp in (4_000_000_000, 5_800_000_000, 6_200_000_000, 7_000_000_000, 9_000_000_000):
            writer.add_audio(np.full(30, timestamp // 100_000_000, np.int16).tobytes(), timestamp)
    path.with_suffix(".gcsv").write_text(
        "GYROFLOW IMU LOG\ntimestamp,100000\ntscale,0.25\ngscale,1\nt,gx,gy,gz\n"
        + "".join(f"{i},1,2,3,\n" for i in range(17))
    )
    return path


def _export(
    source: Path,
    output: Path,
    *,
    frame_range: tuple[int, int] | None = None,
    force: bool = False,
    stop: Callable[[], bool] | None = None,
) -> ExportResult:
    from mcrawsome.output.mcraw_export import export_mcraw

    return export_mcraw(
        source,
        output,
        build_recipe([{"output": {"kind": "mcraw"}}]),
        frame_range=frame_range,
        force=force,
        stop=stop,
    )


def _records(path: Path) -> list[tuple[bytes, bytes]]:
    with path.open("rb") as file:
        index = read_index(file)
        return [read_frame_record(file, frame.offset) for frame in index.frames]


def _cut_index(path: Path) -> None:
    with path.open("r+b") as file:
        file.seek(-8, 2)
        (offset,) = struct.unpack("<q", file.read(8))
        file.truncate(offset - 8)


@pytest.mark.fixture
def test_cli_trim_copies_compressed_frame_metadata_and_gyro(
    fixture_10bit_rotated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_decode(*args: object) -> None:
        pytest.fail("MCRAW copying must not decode RAW frames")

    monkeypatch.setattr("mcrawsome._raw.decode", no_decode)
    result = CliRunner().invoke(
        cli,
        [
            "export",
            str(fixture_10bit_rotated),
            "--to",
            "mcraw",
            "--start",
            "1f",
            "--duration",
            "1f",
            "--output",
            str(tmp_path),
            "--workers",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    output = tmp_path / f"{fixture_10bit_rotated.stem}_1-1.mcraw"
    assert _records(output) == [_records(fixture_10bit_rotated)[1]]
    with Clip(output) as copied, Clip(fixture_10bit_rotated) as original:
        assert not copied.recovered
        assert copied.frame_count == 1
        assert copied.timestamps_ns[0] == original.timestamps_ns[1]
        assert copied.audio_chunk_count > 0
    gyro = read_gcsv(output.with_suffix(".gcsv"))
    original_gyro = read_gcsv(fixture_10bit_rotated.with_suffix(".gcsv"))
    assert len(gyro.times_s) > 0
    assert len(gyro.times_s) < len(original_gyro.times_s)
    assert set(gyro.times_s) <= set(original_gyro.times_s)
    assert gyro.header == original_gyro.header
    assert sidecar_path(output).exists()
    assert not WorkFolder(output).path.exists()


def test_full_repair_has_an_index_and_all_complete_frames_and_audio(source: Path, tmp_path: Path) -> None:
    records = _records(source)
    _cut_index(source)
    with Clip(source) as recovered:
        assert recovered.recovered
    result = CliRunner().invoke(cli, ["export", str(source), "--to", "mcraw", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    output = tmp_path / "source_repaired.mcraw"
    with Clip(output) as repaired:
        assert not repaired.recovered
        assert repaired.frame_count == 4
        assert repaired.audio_chunk_count == 5
        assert list(repaired.timestamps_ns) == [5_000_000_000 + i * 1_000_000_000 for i in range(4)]
    assert _records(output) == records
    assert "Recovered:" in result.output


def test_trim_copies_only_audio_chunks_that_overlap(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "slice.mcraw"
    _export(source, output, frame_range=(1, 1))
    with output.open("rb") as file:
        index = read_index(file)
        assert [audio.timestamp_ns for audio in index.audio] == [5_800_000_000, 6_200_000_000]
        assert [int(read_audio_chunk(file, audio.offset)[1][0]) for audio in index.audio] == [58, 62]
    lines = output.with_suffix(".gcsv").read_text().splitlines()
    assert lines[5:] == [f"{i},1,2,3," for i in range(4, 9)]


def test_direct_log_is_refused_while_planning(tmp_path: Path) -> None:
    video = tmp_path / "video_Davinci_Davinci-WG.mp4"
    video.touch()
    result = CliRunner().invoke(cli, ["export", str(video), "--to", "mcraw", "--dry-run"])
    assert result.exit_code != 0
    assert "MCRAW needs a RAW clip" in result.output


def test_processing_settings_produce_one_ignored_note(source: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "export",
            str(source),
            "--to",
            "mcraw",
            "--output",
            str(tmp_path),
            "--develop.exposure",
            "2",
            "--motion.stabilise",
            "on",
        ],
    )
    assert result.exit_code == 0, result.output
    assert " ".join(result.output.split()).count("development and stabilisation do not apply") == 1
    assert _records(tmp_path / "source_repaired.mcraw") == _records(source)


@pytest.mark.parametrize("companion", ["output", "gyro", "sidecar"])
def test_existing_outputs_need_force(source: Path, tmp_path: Path, companion: str) -> None:
    output = tmp_path / "copy.mcraw"
    paths = {"output": output, "gyro": output.with_suffix(".gcsv"), "sidecar": sidecar_path(output)}
    paths[companion].write_bytes(b"keep")
    with pytest.raises(McError) as caught:
        _export(source, output)
    assert caught.value.code == "output-exists"
    assert paths[companion].read_bytes() == b"keep"
    _export(source, output, force=True)
    assert paths[companion].read_bytes() != b"keep"


def test_force_cannot_replace_the_source(source: Path) -> None:
    before = source.read_bytes()
    with pytest.raises(McError, match="input"):
        _export(source, source, force=True)
    assert source.read_bytes() == before


def test_failure_verifying_keeps_the_previous_output(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.output import mcraw_export

    output = tmp_path / "copy.mcraw"
    output.write_bytes(b"previous")

    def fail(*args: object) -> None:
        raise McError("verification failed", code="verify-failed")

    monkeypatch.setattr(mcraw_export, "_verify", fail)
    with pytest.raises(McError, match="verification failed"):
        _export(source, output, force=True)
    assert output.read_bytes() == b"previous"
    assert WorkFolder(output).publish_path.exists()


def test_paused_copy_can_be_retried(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "copy.mcraw"

    def stop() -> bool:
        return True

    with pytest.raises(McError) as caught:
        _export(source, output, stop=stop)
    assert caught.value.code == "paused"
    assert not output.exists()
    result = _export(source, output)
    assert "interrupted MCRAW copy; starting again" in result.warnings
    assert not any("settings changed" in note for note in result.warnings)
    assert _records(output) == _records(source)


def test_sidecar_records_copied_range(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "copy.mcraw"
    _export(source, output, frame_range=(1, 2))
    sidecar = tomllib.loads(sidecar_path(output).read_text())
    assert sidecar["recipe"]["output"]["kind"] == "mcraw"
    assert sidecar["measurements"]["first_frame"] == 1
    assert sidecar["measurements"]["frame_count"] == 2


@pytest.mark.local
def test_local_unfinalised_recording_is_repaired_with_an_index() -> None:
    folder = Path(os.environ.get("MCRAWSOME_LOCAL_SAMPLES", Path.home() / "Downloads" / "motioncam"))
    source = folder / "261010_135132_VIDEO_25mm.mcraw"
    if not source.is_file():
        pytest.skip("local unfinished recording not present (set MCRAWSOME_LOCAL_SAMPLES)")
    cache = Path.home() / ".cache" / "mcrawsome-mcraw-output"
    cache.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=cache))
    try:
        output = scratch / "repaired.mcraw"
        _export(source, output)
        with Clip(source) as original, Clip(output) as repaired:
            assert original.recovered
            assert not repaired.recovered
            assert repaired.frame_count == original.frame_count
            np.testing.assert_array_equal(repaired.timestamps_ns, original.timestamps_ns)
            assert repaired.audio_chunk_count == original.audio_chunk_count
    finally:
        shutil.rmtree(scratch)


@pytest.mark.parametrize("problem", ["frames", "timestamps", "audio", "index"])
def test_verification_rejects_an_incorrect_copy(source: Path, tmp_path: Path, problem: str) -> None:
    from mcrawsome.output.mcraw_export import _verify

    output = tmp_path / "copy.mcraw"
    _export(source, output)
    expected = (4, 5_000_000_000, 8_000_000_000)
    audio = 5
    if problem == "frames":
        expected = (3, expected[1], expected[2])
    elif problem == "timestamps":
        expected = (4, expected[1] + 1, expected[2])
    elif problem == "audio":
        audio = 6
    else:
        _cut_index(output)
    with pytest.raises(McError) as caught:
        _verify(output, expected, audio)
    assert caught.value.code == "verify-failed"


def test_source_without_gyro_removes_an_old_gyro_only_with_force(source: Path, tmp_path: Path) -> None:
    source.with_suffix(".gcsv").unlink()
    output = tmp_path / "copy.mcraw"
    gyro = output.with_suffix(".gcsv")
    gyro.write_bytes(b"previous")
    with pytest.raises(McError):
        _export(source, output)
    assert gyro.read_bytes() == b"previous"
    _export(source, output, force=True)
    assert not gyro.exists()


def test_gyro_slice_preserves_rows_either_side_for_interpolation(source: Path, tmp_path: Path) -> None:
    from mcrawsome.io.gcsv import write_gcsv_slice

    output = tmp_path / "slice.gcsv"
    write_gcsv_slice(source.with_suffix(".gcsv"), output, 1.1, 1.9)
    assert output.read_text().splitlines()[5:] == [f"{i},1,2,3," for i in range(4, 9)]
    log = read_gcsv(output)
    assert log.times_s[0] < 1.1
    assert log.times_s[-1] > 1.9


def _break_gyro(source: Path, kind: str) -> None:
    path = source.with_suffix(".gcsv")
    text = path.read_text()
    if kind == "no-tscale":
        text = text.replace("tscale,0.25\n", "")
    elif kind == "truncated-row":
        text = text.rstrip("\n").rsplit("\n", 1)[0] + "\n16,1\n"
    elif kind == "no-timestamp":
        text = text.replace("timestamp,100000\n", "")
    path.write_text(text)


@pytest.mark.parametrize("kind", ["no-tscale", "truncated-row", "no-timestamp"])
@pytest.mark.parametrize("frame_range", [None, (1, 2)])
def test_unusable_gyro_log_is_left_out_with_a_note(
    source: Path, tmp_path: Path, kind: str, frame_range: tuple[int, int] | None
) -> None:
    if kind == "no-timestamp" and frame_range is None:
        pytest.skip("a whole copy never needs the log's start time")
    _break_gyro(source, kind)
    output = tmp_path / "copy.mcraw"
    result = _export(source, output, frame_range=frame_range)
    expected = _records(source) if frame_range is None else _records(source)[1:3]
    assert _records(output) == expected
    assert not output.with_suffix(".gcsv").exists()
    assert sidecar_path(output).exists()
    assert any("gyro log" in note and "left out" in note for note in result.warnings)


def test_ignored_note_only_when_processing_settings_are_set(source: Path, tmp_path: Path) -> None:
    plain = _export(source, tmp_path / "plain.mcraw")
    assert not any("do not apply" in note for note in plain.warnings)
    assert "note" not in tomllib.loads(sidecar_path(tmp_path / "plain.mcraw").read_text())["measurements"]
    from mcrawsome.output.mcraw_export import export_mcraw

    recipe = build_recipe([{"output": {"kind": "mcraw"}, "develop": {"exposure": 2.0}}])
    result = export_mcraw(source, tmp_path / "set.mcraw", recipe)
    assert sum("do not apply" in note for note in result.warnings) == 1
    measurements = tomllib.loads(sidecar_path(tmp_path / "set.mcraw").read_text())["measurements"]
    assert "do not apply" in measurements["note"]


def _crash_after_first_rename(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    real = Path.replace
    calls: list[Path] = []

    def replace(self: Path, target: str | os.PathLike[str]) -> Path:
        if self.parent.name.endswith(".mcrawsome-work") and not Path(target).parent.name.endswith("-work"):
            if len(calls) >= count:
                raise KeyboardInterrupt
            calls.append(self)
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)


@pytest.mark.parametrize("count", [1, 2])
def test_rerun_after_an_interrupted_publish_needs_no_force(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    output = tmp_path / "copy.mcraw"
    with monkeypatch.context() as patch:
        _crash_after_first_rename(patch, count)
        with pytest.raises(KeyboardInterrupt):
            _export(source, output)
    result = _export(source, output)
    assert _records(output) == _records(source)
    assert read_gcsv(output.with_suffix(".gcsv")).times_s.size == 17
    assert sidecar_path(output).exists()
    assert not WorkFolder(output).path.exists()
    assert any("interrupted" in note for note in result.warnings)


def test_gyro_log_with_a_stray_invalid_byte_reads_and_slices(source: Path, tmp_path: Path) -> None:
    from mcrawsome.io.gcsv import write_gcsv_slice

    path = source.with_suffix(".gcsv")
    path.write_bytes(path.read_bytes().replace(b"gscale,1\n", b"gscale,1\nnote,caf\xe9\n"))
    assert len(read_gcsv(path).times_s) == 17
    write_gcsv_slice(path, tmp_path / "slice.gcsv", 1.1, 1.9)
    assert len(read_gcsv(tmp_path / "slice.gcsv").times_s) == 5


def test_ctrl_c_says_an_mcraw_copy_starts_again(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.cli import processing as main_module
    from mcrawsome.output.mcraw_export import export_mcraw

    def interrupted(*args: object, **kwargs: object) -> ExportResult:
        def press_ctrl_c(phase: str, done: int, total: int) -> None:
            if phase == "rendering":
                raise KeyboardInterrupt

        return export_mcraw(*args, **kwargs | {"progress": press_ctrl_c})  # type: ignore[arg-type]

    monkeypatch.setattr(main_module, "export_mcraw", interrupted)
    out = tmp_path / "out"
    result = CliRunner().invoke(cli, ["export", str(source), "--to", "mcraw", "--output", f"{out}/"])
    assert result.exit_code == 130, result.output
    assert "resume" not in result.output
    assert "an MCRAW copy starts again" in result.output


def test_the_sidecar_records_the_absolute_input_path(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = _export(Path(source.name), Path("out.mcraw"))
    [entry] = tomllib.loads(result.sidecar.read_text())["inputs"]
    assert entry["path"] == str(source)
    assert result.output == tmp_path / "out.mcraw"
