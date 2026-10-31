import json
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.analysis.preview_overlay import gyro_charts, measure_overlays
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerWriter, read_frame_record, read_index
from mcrawsome.io.gyro import GyroLog
from mcrawsome.recipe.schema import Recipe


def test_overlay_maps_capture_to_preview_frames_without_decoding_or_writing(
    tiny_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = tiny_raw.read_bytes()
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("overlay decoded pixels"))
    monkeypatch.setattr(Clip, "audio", lambda *_: pytest.fail("overlay read audio"))
    recipe = Recipe.model_validate({"output": {"frame_rate": "48"}})
    report = measure_overlays(tiny_raw, recipe, threading.Event())
    assert report.frames == 6
    iso = next(chart for chart in report.charts if chart.key == "iso")
    assert [p.frame for p in iso.series[0].points] == list(range(6))
    assert iso.series[0].points[0].value == iso.series[0].points[1].value
    assert len([p for chart in report.charts for line in chart.series for p in line.points]) <= 6 * 24
    assert tiny_raw.read_bytes() == before


def test_gyro_uses_recorded_receive_clock_and_explicit_sync_offset(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synced.mcraw"
    with tiny_raw.open("rb") as stream:
        index = read_index(stream)
        with ContainerWriter(source, index.metadata) as writer:
            for i, ref in enumerate(index.frames):
                data, encoded = read_frame_record(stream, ref.offset)
                meta = json.loads(encoded)
                meta.update(recvdTimestampMs=1_700_000_000_000 + i * 100, exposureTime=0)
                writer.add_frame(5_000_000_000 + i * 100_000_000, data, json.dumps(meta).encode())
    source.with_suffix(".gcsv").write_text(
        "GYROFLOW IMU LOG\ntimestamp,1700000000000\ntscale,1\ngscale,1\nt,gx,gy,gz\n"
        + "".join(f"{i / 100},{i / 100},0,0\n" for i in range(51))
    )
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("overlay fitted image motion"))
    recipe = Recipe.model_validate({"camera": {"sync_ms": 100, "readout_ms": 0}})
    report = measure_overlays(source, recipe, threading.Event())
    chart = next(chart for chart in report.charts if chart.key == "gyro")
    assert chart.series[0].points[0].value == pytest.approx(np.rad2deg(0.1))
    assert chart.series[0].points[2].value == pytest.approx(np.rad2deg(0.3))
    assert "+100 ms" in chart.help
    assert "Not visually fitted" in chart.help


def test_gyro_gap_is_not_drawn_as_continuous_motion() -> None:
    log = GyroLog(np.array([0, 0.01, 1, 1.01]), np.ones((4, 3)), None, 0, "XYZ")
    chart = gyro_charts(log, np.array([0, 0.5, 1]), "test", threading.Event())[0]
    assert [p.value for p in chart.series[0].points] == [
        pytest.approx(np.rad2deg(1)),
        None,
        pytest.approx(np.rad2deg(1)),
    ]


def test_unlinkable_gyro_keeps_other_charts_and_explains_the_problem(tiny_raw: Path) -> None:
    tiny_raw.with_suffix(".gcsv").write_text(
        "GYROFLOW IMU LOG\ntscale,1\ngscale,1\nt,gx,gy,gz\n0,1,2,3\n0.01,1,2,3\n"
    )
    report = measure_overlays(tiny_raw, Recipe(), threading.Event())
    assert any(chart.key == "timing" for chart in report.charts)
    assert not any(chart.key == "gyro" for chart in report.charts)
    assert "cannot be linked" in report.notes[0]


def test_shake_blur_overlays_and_analytics_come_from_the_gyro_log(
    tmp_path: Path,
    make_mcraw: Callable[..., Path],
    make_gcsv: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcrawsome.analysis.capture_plot import capture_csv, measure_capture

    source = make_mcraw(tmp_path / "shaky.mcraw", [i * 41_666_667 for i in range(120)])
    make_gcsv(source.with_suffix(".gcsv"), 120, shake_deg_s=40)
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("blur decoded pixels"))
    report = measure_overlays(source, Recipe(), threading.Event())
    blur = next(chart for chart in report.charts if chart.key == "blur")
    values = [point.value for point in blur.series[0].points if point.value is not None]
    assert values
    assert max(values) > 0.5  # pixels of a 128-pixel-wide synthetic frame
    assert "intended path" in blur.help
    capture = measure_capture(source, threading.Event())
    series = next(item for item in capture.series if item.key == "blur")
    assert series.unit == "px"
    assert series.samples == 120
    header, first = "".join(capture_csv(source, threading.Event())).splitlines()[:2]
    assert float(first.split(",")[header.split(",").index("blur")]) > 0
    source.with_suffix(".gcsv").unlink()
    charts = measure_overlays(source, Recipe(), threading.Event()).charts
    assert not any(chart.key == "blur" for chart in charts)
    assert not any(item.key == "blur" for item in measure_capture(source, threading.Event()).series)
