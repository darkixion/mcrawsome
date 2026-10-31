import json
import threading
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mcrawsome.analysis import capture_plot
from mcrawsome.analysis.capture_plot import MAX_METADATA_FRAMES, measure_capture, sample_frames
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerWriter, read_frame_record, read_index


def test_capture_graphs_read_metadata_only_and_keep_units_and_missing_values(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "capture.mcraw"
    with tiny_raw.open("rb") as stream:
        index = read_index(stream)
        with ContainerWriter(source, index.metadata) as writer:
            for i, frame in enumerate(index.frames):
                data, encoded = read_frame_record(stream, frame.offset)
                metadata = json.loads(encoded)
                metadata.update(
                    iso=100 * (i + 1),
                    exposureTime=(i + 1) * 2_000_000,
                    focusDistance=[None, 0, 0.5][i],
                    dynamicBlackLevel=[i, i + 1, i + 2, i + 3],
                    dynamicWhiteLevel=1023 + i,
                )
                writer.add_frame(frame.timestamp_ns, data, json.dumps(metadata).encode())
    before = source.read_bytes()
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("analytics decoded RAW"))
    monkeypatch.setattr(Clip, "audio", lambda *_: pytest.fail("analytics read audio"))
    report = measure_capture(source, threading.Event())
    assert report.frames == report.sampled_frames == 3
    assert report.unreadable_frames == 0
    assert report.timing is not None
    assert report.timing.intervals == 2
    fields = {item.key: item for item in report.series}
    assert [point.value for point in fields["iso"].points] == [100, 200, 300]
    assert fields["shutter"].unit == "ms"
    assert [point.value for point in fields["shutter"].points] == [2, 4, 6]
    assert fields["focus"].unit == "dioptres"
    assert [point.value for point in fields["focus"].points] == [None, 0, 0.5]
    assert [point.value for point in fields["black"].points] == [1.5, 2.5, 3.5]
    assert [point.frame for point in fields["white"].points] == [0, 1, 2]
    assert source.read_bytes() == before


@pytest.mark.parametrize("count", [0, 1, 2, 256, 257, 100_000, 10**12])
def test_metadata_sampling_is_bounded_unique_and_includes_endpoints(count: int) -> None:
    frames = sample_frames(count)
    assert len(frames) == min(count, MAX_METADATA_FRAMES)
    assert frames == sorted(set(frames))
    if count:
        assert frames[0] == 0
        assert frames[-1] == count - 1


class FakeClip:
    def __init__(self, path: Path, count: int = 1000) -> None:
        self.path = path  # a real file: frame values are kept by its path and stamp
        path.touch()
        self.frame_count = count
        self.timestamps_ns = 10**18 + np.arange(count, dtype=np.int64) * 40_000_000
        self.read: list[int] = []
        self.closed = False
        self.stop: threading.Event | None = None

    def __enter__(self) -> "FakeClip":
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def check_unchanged(self) -> None:
        pass

    def raw_frame_metadata(self, index: int) -> dict[str, Any]:
        self.read.append(index)
        if self.stop and len(self.read) == 3:
            self.stop.set()
        return {"iso": index + 1, "focusDistance": float("nan"), "exposureTime": -1}

    def frame_metadata_json(self, index: int) -> bytes:
        return json.dumps(self.raw_frame_metadata(index)).encode()


def test_long_clip_reads_all_metadata_and_preserves_timing_outliers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clip = FakeClip(tmp_path / "clip.mcraw")
    clip.timestamps_ns[500:] += 80_000_000
    monkeypatch.setattr(capture_plot, "Clip", lambda _, **kwargs: clip)
    report = measure_capture(Path("unused.mcraw"), threading.Event())
    assert clip.closed
    assert len(clip.read) == 1000
    assert clip.read == list(range(1000))
    assert report.timing is not None
    assert report.timing.median_ms == 40
    assert report.timing.maximum_ms == 120
    assert report.timing.dropped == 2
    assert [series.key for series in report.series] == ["iso"]
    assert len(report.series[0].points) <= 512
    assert report.series[0].samples == 1000
    assert report.series[0].minimum == 1
    assert report.series[0].maximum == 1000
    assert "NaN" not in report.model_dump_json()


def test_invalid_timing_does_not_hide_capture_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clip = FakeClip(tmp_path / "clip.mcraw", 3)
    clip.timestamps_ns[:] = 10**18
    monkeypatch.setattr(capture_plot, "Clip", lambda _, **kwargs: clip)
    report = measure_capture(Path("unused.mcraw"), threading.Event())
    assert report.timing is None
    assert report.timing_error
    assert report.series[0].points[0].value == 1


def test_capture_read_cancels_between_records_and_closes_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clip = FakeClip(tmp_path / "clip.mcraw")
    clip.stop = threading.Event()
    monkeypatch.setattr(capture_plot, "Clip", lambda _, **kwargs: clip)
    with pytest.raises(CancelledError):
        measure_capture(Path("unused.mcraw"), clip.stop)
    assert clip.closed
    assert len(clip.read) == 3


def test_actual_neutrals_four_bayer_noise_pairs_and_map_gains_are_preserved() -> None:
    from mcrawsome.analysis.capture_plot import capture_values, shading_map

    metadata: dict[str, Any] = {
        "asShotNeutral": [0.5, 1, 0.75],
        "noiseProfile": [1e-5, 1e-8, 2e-5, 2e-8, 3e-5, 3e-8, 4e-5, 4e-8],
        "lensShadingMapWidth": 2,
        "lensShadingMapHeight": 1,
        "lensShadingMap": [[1, 3], [2, 4], [3, 5], [4, 6]],
        "dynamicBlackLevel": [64, 65, 66, 67],
        "dynamicWhiteLevel": 4095,
    }
    values = capture_values(metadata)
    assert [values[f"neutral{i}"] for i in range(3)] == [0.5, 1, 0.75]
    assert [values[f"shading{i}"] for i in range(4)] == [2, 3, 4, 5]
    assert [values[f"noise_offset{i}"] for i in range(4)] == [1e-8, 2e-8, 3e-8, 4e-8]
    assert [values[f"black{i}"] for i in range(4)] == [64, 65, 66, 67]
    assert values["white"] == 4095
    heatmap = shading_map(metadata, 30)
    assert heatmap is not None
    assert heatmap.frame == 30
    assert heatmap.channels[3] == [4, 6]  # no rotation, crop, normalisation or channel averaging
    metadata["noiseProfile"] = [1, 2, 3, 4, 5, 6]
    assert "noise_scale3" not in capture_values(metadata)
    metadata["lensShadingMap"][0][0] = float("nan")
    assert shading_map(metadata, 0) is None


def test_unsampled_single_frame_spike_survives_graph_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clip = FakeClip(tmp_path / "clip.mcraw")
    monkeypatch.setattr(capture_plot, "Clip", lambda _, **kwargs: clip)
    original = clip.raw_frame_metadata

    def record(index: int) -> dict[str, Any]:
        result = original(index)
        result["iso"] = 100_000 if index == 501 else 100
        return result

    monkeypatch.setattr(clip, "raw_frame_metadata", record)
    result = measure_capture(Path("unused.mcraw"), threading.Event())
    series = result.series[0]
    assert series.maximum == 100_000
    assert any(point.maximum == 100_000 for point in series.points)
    assert result.sampled_frames == result.frames == 1000


def test_csv_has_every_record_exact_timestamps_and_empty_missing_values(tiny_raw: Path) -> None:
    import csv
    import io

    from mcrawsome.analysis.capture_plot import capture_csv

    rows = list(csv.DictReader(io.StringIO("".join(capture_csv(tiny_raw, threading.Event())))))
    with Clip(tiny_raw) as clip:
        assert len(rows) == clip.frame_count
        assert [int(row["timestamp_ns"]) for row in rows] == clip.timestamps_ns.tolist()
        assert [int(row["frame"]) for row in rows] == [1, 2, 3]
        assert float(rows[0]["neutral0"]) == clip.raw_frame_metadata(0)["asShotNeutral"][0]
