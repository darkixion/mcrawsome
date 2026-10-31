"""Offline place names and time zones near a recorded GPS fix.

The cities and their time zones come from Debian's GNOME location database (libgweather-4-common's
Locations.xml), read as plain XML. Nothing is looked up online, and a place is only ever a suggestion.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

LOCATIONS = Path("/usr/share/libgweather-4/Locations.xml")
NEAR_KM = 50.0  # a place name further than this is not suggested
EARTH_KM = 6371.0


class Place(BaseModel):
    name: str
    region: str | None = Field(None, description="State or province, where the database has one")
    country: str
    zone: str | None = Field(None, description="The IANA time zone there")
    distance_km: float

    @property
    def label(self) -> str:
        parts = [
            self.name,
            *([self.region] if self.region and self.region != self.name else []),
            self.country,
        ]
        return ", ".join(parts)


@dataclass(frozen=True)
class _City:
    name: str
    region: str | None
    country: str
    zone: str | None
    latitude: float
    longitude: float


def _hint(element: ET.Element, inherited: str | None) -> str | None:
    hint = element.findtext("tz-hint")
    if hint:
        return hint.strip()
    zones = [zone.get("id") for zone in element.findall("timezones/timezone") if zone.get("id")]
    return zones[0] if len(zones) == 1 else inherited


@lru_cache(maxsize=2)
def _cities(path: Path, size: int, modified: int) -> tuple[_City, ...]:
    del size, modified  # the cache key: a changed database is read again
    cities: list[_City] = []

    def walk(element: ET.Element, country: str | None, region: str | None, zone: str | None) -> None:
        for child in element:
            if child.tag == "country":
                walk(child, child.findtext("_name"), None, _hint(child, None))
            elif child.tag in ("region", "state"):
                state = child.findtext("_name") if child.tag == "state" else region
                walk(child, country, state, _hint(child, zone))
            elif child.tag == "city" and country:
                coordinates = (child.findtext("coordinates") or "").split()
                name = child.findtext("_name") or child.findtext("name")
                if name and len(coordinates) == 2:
                    latitude, longitude = (float(value) for value in coordinates)
                    cities.append(_City(name, region, country, _hint(child, zone), latitude, longitude))

    walk(ET.parse(path).getroot(), None, None, None)
    return tuple(cities)


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = (math.radians(value) for value in (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def nearest(
    latitude: float, longitude: float, *, path: Path = LOCATIONS, within_km: float = NEAR_KM
) -> Place | None:
    """The database's city nearest the fix, when one lies within `within_km`; None without the database."""
    try:
        stat = path.stat()
        cities = _cities(path, stat.st_size, stat.st_mtime_ns)
    except (OSError, ET.ParseError, ValueError):
        return None
    best: tuple[float, _City] | None = None
    for city in cities:
        distance = _distance_km((latitude, longitude), (city.latitude, city.longitude))
        if best is None or distance < best[0]:
            best = (distance, city)
    if best is None or best[0] > within_km:
        return None
    distance, city = best
    return Place(
        name=city.name,
        region=city.region,
        country=city.country,
        zone=city.zone,
        distance_km=round(distance, 1),
    )


def recording_zone(gps: tuple[float, float, float | None] | None, *, path: Path = LOCATIONS) -> str | None:
    """The time zone where a clip was recorded, from its GPS fix and the nearest city (within 300 km,
    so a fix at sea or in a remote place still finds its zone); None without a fix or database."""
    if gps is None:
        return None
    place = nearest(gps[0], gps[1], path=path, within_km=300.0)
    return place.zone if place is not None else None
