"""GNOME's search finds recordings by name and by the words the Library's search matches, from the
library MCRAWsome last scanned, without its engine; opening a result starts the window in its own unit."""

import configparser
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mcrawsome import search_provider
from mcrawsome.library.annotations import ClipAnnotations, write_annotations
from mcrawsome.library.scan import clip_id
from mcrawsome.packaging import packaging_dir
from mcrawsome.search_provider import Search, thumbnail


def day(captured: str) -> str:
    """The date a result gives, in this computer's time zone: 25 October 2026."""
    when = datetime.fromisoformat(captured).astimezone()
    return f"{when.day} {when:%B %Y}"


def library(tmp_path: Path, clips: dict[str, dict[str, Any]]) -> Path:
    """A library record such as a scan writes, of recordings made here."""
    files = {}
    for name, facts in clips.items():
        path = tmp_path / name
        path.write_bytes(name.encode())
        files[str(path)] = {"id": clip_id(path), **facts}
    record = tmp_path / "state/library.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"version": 7, "files": files}))
    return record


@pytest.fixture
def search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Search:
    monkeypatch.setenv("MCRAWSOME_STATE_DIR", str(tmp_path / "state"))
    record = library(
        tmp_path,
        {
            "261025_020818_VIDEO_25mm.mcraw": {
                "captured": "2026-10-25T12:08:18+00:00",
                "duration_s": 14.3,
                "camera": "Google Pixel 10 Pro XL",
            },
            "261018_170320_VIDEO_25mm.mcraw": {"captured": "2026-10-18T12:03:20+00:00", "duration_s": 75.0},
        },
    )
    harbour = tmp_path / "261018_170320_VIDEO_25mm.mcraw"
    details = ClipAnnotations(title="Harbour at dusk", keywords=["Boats"], location="Whitby")
    write_annotations(harbour, clip_id(harbour), details, "")
    return Search(record)


def test_names_and_clip_details_are_searched_case_blind_with_every_term_required(search: Search) -> None:
    newest, harbour = (str(Path(search.record).parents[1] / name) for name in (
        "261025_020818_VIDEO_25mm.mcraw", "261018_170320_VIDEO_25mm.mcraw"
    ))  # fmt: skip
    assert search.results(["VIDEO"]) == [newest, harbour]  # newest first
    assert search.results(["harbour"]) == [harbour]
    assert search.results(["boats", "WHITBY"]) == [harbour]
    assert search.results(["boats", "london"]) == []
    assert search.results(["  "]) == []


def test_narrowing_a_search_keeps_gnomes_order(search: Search) -> None:
    folder = Path(search.record).parents[1]
    newest, harbour = (
        str(folder / name) for name in ("261025_020818_VIDEO_25mm.mcraw", "261018_170320_VIDEO_25mm.mcraw")
    )
    assert search.results(["video"], [harbour, newest]) == [harbour, newest]
    assert search.results(["dusk"], [newest, harbour]) == [harbour]


def test_results_say_what_the_recording_is(
    search: Search, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = Path(search.record).parents[1]
    newest, harbour = (
        str(folder / name) for name in ("261025_020818_VIDEO_25mm.mcraw", "261018_170320_VIDEO_25mm.mcraw")
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    uri = "file://" + newest
    made = (
        tmp_path
        / "cache/thumbnails/large"
        / (hashlib.md5(uri.encode(), usedforsecurity=False).hexdigest() + ".png")
    )
    made.parent.mkdir(parents=True)
    made.write_bytes(b"png")
    assert search.metas([newest, harbour, "/elsewhere.mcraw"]) == [
        {
            "id": newest,
            "name": "261025_020818_VIDEO_25mm.mcraw",
            "description": f"0:14, {day('2026-10-25T12:08:18+00:00')}, Google Pixel 10 Pro XL",
            "gicon": str(made),
        },
        {
            "id": harbour,
            "name": "Harbour at dusk",
            "description": f"1:15, {day('2026-10-18T12:03:20+00:00')}",
            "gicon": "video-x-generic",
        },
    ]


def test_thumbnails_are_found_by_the_uri_glib_would_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = "/media/Shoots & takes/Harbour (dusk), 2.mcraw"
    uri = "file:///media/Shoots%20&%20takes/Harbour%20(dusk),%202.mcraw"
    made = (
        tmp_path
        / "thumbnails/normal"
        / (hashlib.md5(uri.encode(), usedforsecurity=False).hexdigest() + ".png")
    )
    made.parent.mkdir(parents=True)
    made.write_bytes(b"png")
    assert thumbnail(path) == str(made)


def test_a_rescanned_library_is_read_again_and_missing_files_are_left_out(search: Search) -> None:
    folder = Path(search.record).parents[1]
    assert len(search.results(["video"])) == 2
    (folder / "261025_020818_VIDEO_25mm.mcraw").unlink()
    search.record.write_text(search.record.read_text() + " ")  # a rescan rewrites it
    assert search.results(["video"]) == [str(folder / "261018_170320_VIDEO_25mm.mcraw")]


def test_opening_a_result_starts_the_window_in_its_own_unit(
    search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[list[str], str]] = []
    monkeypatch.setattr(search_provider, "spawn", lambda command, role: started.append((command, role)))
    monkeypatch.setattr(search_provider, "launcher", lambda: "/opt/MCRAWsome.AppImage")
    harbour = str(Path(search.record).parents[1] / "261018_170320_VIDEO_25mm.mcraw")
    search.activate(harbour)
    search.activate("/not/a/result.mcraw")
    search.launch()
    assert started == [
        (["/opt/MCRAWsome.AppImage", "gui", "--", harbour], "window"),
        (["/opt/MCRAWsome.AppImage", "gui", "--view", "library"], "window"),
    ]


def test_the_service_and_gnome_shell_agree_on_its_name() -> None:
    methods = [method.get("name") for method in ET.fromstring(search_provider.INTERFACE).iter("method")]
    assert methods == [
        "GetInitialResultSet",
        "GetSubsearchResultSet",
        "GetResultMetas",
        "ActivateResult",
        "LaunchSearch",
    ]
    shell = configparser.ConfigParser(interpolation=None)
    shell.read(packaging_dir() / "linux/mcrawsome-search-provider.ini")
    provider = shell["Shell Search Provider"]
    assert (provider["BusName"], provider["ObjectPath"], provider["DesktopId"], provider["Version"]) == (
        search_provider.BUS_NAME,
        search_provider.OBJECT_PATH,
        "mcrawsome.desktop",
        "2",
    )
    bus = configparser.ConfigParser(interpolation=None)
    bus.read(packaging_dir() / f"linux/{search_provider.BUS_NAME}.service")
    assert bus["D-BUS Service"]["Name"] == search_provider.BUS_NAME
    assert bus["D-BUS Service"]["Exec"] == "/usr/bin/mcrawsome search-provider"
    assert os.path.isfile(packaging_dir() / "linux/mcrawsome.desktop")
