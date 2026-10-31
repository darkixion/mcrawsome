"""Offline place names and time zones from a GNOME-format location database."""

from pathlib import Path

import pytest

from mcrawsome.library import places

DATABASE = """<?xml version="1.0" encoding="utf-8"?>
<gweather format="1.0">
  <region><_name>Europe</_name>
    <country><_name>United Kingdom</_name><iso-code>GB</iso-code>
      <timezones><timezone id="Europe/London" /></timezones><tz-hint>Europe/London</tz-hint>
      <city><_name>Bristol</_name><coordinates>51.454513 -2.587910</coordinates></city>
      <city><_name>Bath</_name><coordinates>51.380001 -2.360000</coordinates></city>
    </country>
  </region>
  <region><_name>North America</_name>
    <country><_name>United States</_name><iso-code>US</iso-code>
      <timezones><timezone id="America/New_York" /><timezone id="America/Chicago" /></timezones>
      <state><_name>Alabama</_name><tz-hint>America/Chicago</tz-hint>
        <city><_name>Birmingham</_name><coordinates>33.520661 -86.802490</coordinates></city>
      </state>
      <state><_name>Florida</_name><tz-hint>America/New_York</tz-hint>
        <city><_name>Pensacola</_name><tz-hint>America/Chicago</tz-hint>
          <coordinates>30.421309 -87.216915</coordinates></city>
      </state>
    </country>
  </region>
</gweather>
"""


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "Locations.xml"
    path.write_text(DATABASE)
    return path


def test_the_nearest_city_names_the_place_and_its_time_zone(database: Path) -> None:
    place = places.nearest(51.47, -2.6, path=database)
    assert place is not None
    assert (place.name, place.country, place.zone, place.label) == (
        "Bristol",
        "United Kingdom",
        "Europe/London",
        "Bristol, United Kingdom",
    )
    assert place.distance_km < 3
    alabama = places.nearest(33.5, -86.8, path=database)
    assert alabama is not None
    assert (alabama.label, alabama.zone) == ("Birmingham, Alabama, United States", "America/Chicago")
    pensacola = places.nearest(30.42, -87.2, path=database)
    assert pensacola is not None
    assert pensacola.zone == "America/Chicago"  # the city's own zone wins over its state's


def test_far_fixes_and_a_missing_database_give_no_place(database: Path, tmp_path: Path) -> None:
    assert places.nearest(0.0, 0.0, path=database) is None
    assert places.nearest(51.47, -2.6, path=tmp_path / "missing.xml") is None
    assert places.recording_zone((51.0, -3.5, None), path=database) == "Europe/London"
    assert places.recording_zone(None, path=database) is None


def test_the_installed_database_is_read_when_present() -> None:
    if not places.LOCATIONS.is_file():
        pytest.skip("libgweather-4-common is not installed")
    place = places.nearest(51.5, -2.6)
    assert place is not None
    assert place.zone == "Europe/London"
