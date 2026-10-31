"""Camera reports and recorded positions for chosen clips."""

import csv
import io
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from test_clips_api import listed_clip

from mcrawsome.library.reports import locations
from mcrawsome.library.scan import ClipSummary


def test_a_camera_report_lists_each_clip_with_its_details_and_timecode(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    identity, _ = listed_clip(client, library, make_mcraw)
    details = {
        "title": "Harbour wall",
        "scene": "12B",
        "take": "3",
        "tag": "good-take",
        "notes": "Keep\tthe end.",
    }
    assert client.put(
        f"/api/v1/clips/{identity}/annotations", json={"version": "", "values": details}
    ).is_success
    response = client.post("/api/v1/clips/report", json={"ids": [identity], "format": "csv"})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "camera-report.csv"
    [row] = list(csv.DictReader(io.StringIO(response.json()["content"])))
    assert (row["Name"], row["Title"], row["Scene"], row["Take"], row["Tag"]) == (
        "a.mcraw",
        "Harbour wall",
        "12B",
        "3",
        "Good take",
    )
    assert row["Start"] == "00:00:00:00"  # timecode from zero, as Settings chooses by default
    assert row["End"] == "00:00:00:02"
    ale = client.post("/api/v1/clips/report", json={"ids": [identity], "format": "ale"}).json()
    lines = ale["content"].splitlines()
    assert lines[:2] == ["Heading", "FIELD_DELIM\tTABS"]
    columns = lines[lines.index("Column") + 1].split("\t")
    data = lines[lines.index("Data") + 1].split("\t")
    assert len(data) == len(columns)
    assert dict(zip(columns, data, strict=True))["Comments"] == "Keep the end."  # no tab inside a cell
    gpx = client.post("/api/v1/clips/report", json={"ids": [identity], "format": "gpx"})
    assert gpx.status_code >= 400
    assert "GPS position" in gpx.json()["error"]["message"]


def test_gpx_lists_a_waypoint_for_each_recorded_position(tmp_path: Path) -> None:
    def clip(name: str, position: tuple[float, float] | None) -> ClipSummary:
        return ClipSummary(
            id=None, path=tmp_path / name, paths=[tmp_path / name], name=name, kind="raw", status="ready",
            position=position, place="Bristol, United Kingdom" if position else None,
            captured=datetime(2026, 10, 25, 13, 44, 16, tzinfo=UTC),
        )  # fmt: skip

    content = locations([clip("a.mcraw", (51.4545, -2.5879)), clip("b.mcraw", None)]).content
    assert content.count("<wpt ") == 1
    assert '<wpt lat="51.4545000" lon="-2.5879000"><time>2026-10-25T13:44:16Z</time>' in content
    assert "<name>a.mcraw</name><desc>Bristol, United Kingdom · a.mcraw</desc>" in content


def test_a_trimmed_clip_is_reported_as_its_mount_serves_it(
    client: TestClient, library: Path, make_mcraw: Callable[..., Path]
) -> None:
    import os
    import time

    from mcrawsome.library.reports import _ale_rate

    source = make_mcraw(library / "long.mcraw", [i * 41_666_667 for i in range(48)])
    os.utime(source, (time.time() - 60,) * 2)
    assert client.put("/api/v1/settings", json={"folders": [str(library)]}).status_code == 200
    [clip] = client.get("/api/v1/library").json()["clips"]
    layer = client.get(f"/api/v1/clips/{clip['id']}/recipe").json()["layer"]
    trim = {"start": "0.5s", "duration": "1s"}
    saved = client.put(
        f"/api/v1/clips/{clip['id']}/recipe", json={"version": layer["version"], "values": {"trim": trim}}
    )
    assert saved.status_code == 200, saved.text
    content = client.post("/api/v1/clips/report", json={"ids": [clip["id"]], "format": "csv"}).json()[
        "content"
    ]
    [row] = list(csv.DictReader(io.StringIO(content)))
    assert (row["Start"], row["End"], row["Frames"], row["Duration"]) == (
        "00:00:00:00",
        "00:00:00:23",
        "24",
        "1.00",
    )
    assert _ale_rate(23.792) == "24"
    assert _ale_rate(29.97) == "29.97"
