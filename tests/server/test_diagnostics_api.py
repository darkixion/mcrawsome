import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_clips_api import listed_clip

from mcrawsome.io.clip import Clip, RawFrame


def test_gyro_graph_reads_the_log_without_decoding_raw(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, path = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}/gyro"
    assert client.get(url).status_code == 422
    path.with_suffix(".gcsv").write_text(
        "GYROFLOW IMU LOG\ntscale,1\ngscale,1\nascale,1\nt,gx,gy,gz,ax,ay,az\n"
        "0,1,0,0,0,9.81,0\n0.01,0,2,0,0,9.81,0\n"
    )
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("gyro display decoded RAW"))
    response = client.get(url)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["samples"] == 2
    assert data["sample_rate_hz"] == pytest.approx(100)
    assert data["angular_velocity"][0]["mean"][0] == pytest.approx(57.2957795)
    assert data["acceleration"][0]["mean"][1] == pytest.approx(9.81)
    path.with_suffix(".gcsv").write_text("unreadable log")
    assert client.get(url).status_code == 422


def test_timing_is_metadata_only_and_histogram_is_explicit(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, path = listed_clip(client, library, make_mcraw)
    url = f"/api/v1/clips/{identity}"
    original = Clip.raw_frame
    decoded: list[int] = []

    def decode(self: Clip, index: int) -> RawFrame:
        decoded.append(index)
        return original(self, index)

    monkeypatch.setattr(Clip, "raw_frame", decode)
    result = client.get(url + "/timing")
    assert result.status_code == 200, result.text
    assert result.json()["frames"] == 3
    assert decoded == []
    result = client.get(url + "/raw-histogram")
    assert result.status_code == 200, result.text
    assert decoded == [0]
    assert sum(plane["samples"] for plane in result.json()["planes"]) == 128 * 64
    assert path.is_file()
    for suffix in ("timing", "raw-histogram", "analytics"):
        assert client.get("/api/v1/clips/no-such-clip/" + suffix).status_code == 404


def test_diagnostics_refuse_log_sources(client: TestClient, video: Path, library: Path) -> None:
    import os
    import time

    os.utime(video, (time.time() - 60,) * 2)
    client.put("/api/v1/settings", json={"folders": [str(library)]})
    [clip] = client.get("/api/v1/library").json()["clips"]
    for suffix in ("timing", "raw-histogram"):
        assert client.get(f"/api/v1/clips/{clip['id']}/{suffix}").status_code == 422


def test_disconnected_measurement_stops_the_worker(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import CancelledError

    from fastapi import Request

    from mcrawsome.server.routes import diagnostics

    identity, _ = listed_clip(client, library, make_mcraw)
    stopped = threading.Event()

    def measure(path: Path, stop: threading.Event) -> None:
        assert stop.wait(5)
        stopped.set()
        raise CancelledError

    async def disconnected(self: Request) -> bool:
        return True

    monkeypatch.setattr(diagnostics, "measure_first_frame", measure)
    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    result = client.get(f"/api/v1/clips/{identity}/raw-histogram")
    assert result.status_code == 409, result.text
    assert stopped.wait(5)


def test_a_diagnostic_waits_for_the_one_before_instead_of_being_refused(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    import threading

    identity, _ = listed_clip(client, library, make_mcraw)
    assert isinstance(client.app, FastAPI)
    release = threading.Event()
    running, _ = client.app.state.diagnostics.submit(lambda stop: release.wait(5))
    timer = threading.Timer(0.3, release.set)  # say, a measurement whose client moved to another clip
    timer.start()
    try:
        result = client.get(f"/api/v1/clips/{identity}/timing")  # asked while the worker is busy
    finally:
        release.set()
        timer.cancel()
    assert result.status_code == 200, result.text
    assert running.result(timeout=5) is True


def test_capture_analytics_is_available_without_gyro_and_never_decodes_pixels(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, path = listed_clip(client, library, make_mcraw)
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("analytics decoded RAW"))
    before = path.read_bytes()
    response = client.get(f"/api/v1/clips/{identity}/analytics")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["frames"] == data["sampled_frames"] == 3
    assert data["timing"]["intervals"] == 2
    assert {item["key"] for item in data["series"]} >= {"iso", "shutter", "black", "white"}
    assert path.read_bytes() == before


def test_preview_overlay_is_read_only_and_has_its_own_requested_frame_mapping(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, path = listed_clip(client, library, make_mcraw)
    before = path.read_bytes()
    recipe_path = path.with_suffix(".mcrawsome.toml")
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("overlay decoded RAW"))
    result = client.post(
        f"/api/v1/clips/{identity}/preview-overlays", json={"recipe": {"output": {"frame_rate": "48"}}}
    )
    assert result.status_code == 200, result.text
    assert result.json()["frames"] == 6
    assert result.json()["charts"]
    assert path.read_bytes() == before
    assert not recipe_path.exists()


def test_the_kept_frame_is_planned_with_the_pages_settings_on_its_own_worker(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from mcrawsome.recipe.schema import Recipe
    from mcrawsome.resolve.kept_frame import KeptFrame
    from mcrawsome.server.routes import diagnostics

    identity, path = listed_clip(client, library, make_mcraw)
    asked: list[tuple[Path, Recipe, str, bool]] = []

    def plan(clip: Path, recipe: Recipe, _stop: threading.Event, *, quick: bool = False) -> KeptFrame:
        asked.append((clip, recipe, threading.current_thread().name, quick))
        return KeptFrame(corners=[[0.05, 0.05, 0.95, 0.05, 0.95, 0.95, 0.05, 0.95]] * 3)

    monkeypatch.setattr(diagnostics, "kept_frame", plan)
    sent = {"recipe": {"motion": {"stabilise": "on", "mode": "tripod", "crop": 1.2}}, "quick": True}
    response = client.post(f"/api/v1/clips/{identity}/kept-frame", json=sent)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "corners": [[0.05, 0.05, 0.95, 0.05, 0.95, 0.95, 0.05, 0.95]] * 3,
        "reason": None,
    }
    [(clip, recipe, thread, quick)] = asked
    assert quick
    assert clip == path
    assert (recipe.motion.mode, recipe.motion.crop) == ("tripod", 1.2)
    assert thread.startswith("raw-diagnostics")
    assert not path.with_suffix(".mcrawsome.toml").exists()  # the page's settings are not saved


def test_background_scan_returns_progress_without_blocking_health(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from mcrawsome.analysis import capture_scan
    from mcrawsome.analysis.capture_plot import CaptureAnalytics

    identity, _ = listed_clip(client, library, make_mcraw)
    entered, release = threading.Event(), threading.Event()

    def scan(path: Path, stop: threading.Event, *, progress: Callable[[int, int], None]) -> CaptureAnalytics:
        progress(2, 3)
        entered.set()
        assert release.wait(5)
        return CaptureAnalytics(frames=3, sampled_frames=3, unreadable_frames=0, timing=None, series=[])

    monkeypatch.setattr(capture_scan, "measure_capture", scan)
    url = f"/api/v1/clips/{identity}/analytics-scan"
    try:
        assert client.get(url).json()["status"] == "queued"
        assert entered.wait(5)
        assert client.get(url).json()["completed"] == 2
        assert client.get("/api/v1/health").status_code == 200
    finally:
        release.set()
    assert isinstance(client.app, FastAPI)
    client.app.state.capture_scans._executor.submit(lambda: None).result(timeout=5)
    assert client.get(url).json()["status"] == "ready"


def test_shading_map_and_streamed_csv_are_read_only(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import csv
    import io

    identity, path = listed_clip(client, library, make_mcraw)
    before = path.read_bytes()
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("decoded image"))
    base = f"/api/v1/clips/{identity}"
    result = client.get(base + "/shading-map?frame=0")
    assert result.status_code == 200
    assert len(result.json()["channels"]) == 4
    assert client.get(base + "/shading-map?frame=-1").status_code == 422
    response = client.get(base + "/capture.csv")
    assert response.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(response.text)))) == 3
    assert path.read_bytes() == before
    assert client.get(base + "/analytics").status_code == 200  # streaming lease released


def test_sensor_clipping_is_preview_only(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, path = listed_clip(client, library, make_mcraw)
    recipe_url = f"/api/v1/clips/{identity}/recipe"
    before = client.get(recipe_url).json()["layer"]
    response = client.post(f"/api/v1/clips/{identity}/sensor-clipping", json={"recipe": {}, "index": 0})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["url"].startswith("data:image/png;base64,")
    assert result["frame_index"] == 0
    assert client.get(recipe_url).json()["layer"] == before
    assert not path.with_suffix(".mcrawsome.toml").exists()


def test_motion_reports_movement_blur_and_a_suggested_trim_from_the_gyro_log(
    client: TestClient,
    library: Path,
    make_mcraw: Callable[..., Path],
    make_gcsv: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clip_id, _ = listed_clip(client, library, make_mcraw)
    empty = client.get(f"/api/v1/clips/{clip_id}/motion")
    assert empty.status_code == 200, empty.text
    assert empty.json()["movement"] is None
    assert empty.json()["notes"] == ["Movement, blur and trim suggestions need the clip's gyro log"]
    # Made outside the library and moved in settled, gyro log first: the folder watcher may scan at any
    # moment, and a file whose time changes after a scan has seen it counts as still being copied.
    (library.parent / "staging").mkdir()
    made = make_mcraw(library.parent / "staging/b.mcraw", [i * 41_666_667 for i in range(240)])
    make_gcsv(made.with_suffix(".gcsv"), 240, shake_deg_s=8, jolt_at=9.6)
    os.utime(made, (time.time() - 60,) * 2)
    longer = library / "b.mcraw"
    made.with_suffix(".gcsv").rename(longer.with_suffix(".gcsv"))
    made.rename(longer)
    clip = next(item for item in client.get("/api/v1/library").json()["clips"] if item["name"] == "b.mcraw")
    assert clip["movement"] == "handheld"
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("motion decoded pixels"))
    response = client.get(f"/api/v1/clips/{clip['id']}/motion")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["movement"] == "handheld"
    assert report["shares"]["handheld"] == 1.0
    assert report["blur_median_px"] > 0
    assert report["trim"]["start_s"] == 0
    assert 9.0 < report["trim"]["end_s"] < 9.6
    assert report["trim"]["reasons"][0].startswith("End: a handling jolt")


def test_checks_report_capture_warnings_for_a_listed_clip(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_id, _ = listed_clip(client, library, make_mcraw)
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("checks decoded pixels"))
    response = client.get(f"/api/v1/clips/{clip_id}/checks")
    assert response.status_code == 200, response.text
    # The synthetic clip's audio is silence in both channels: identical, and very quiet.
    assert [check["name"] for check in response.json()["checks"]] == ["Sound level", "Sound channels"]
