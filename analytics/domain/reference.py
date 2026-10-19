import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from analytics.domain.geo import (
    distance_to_polyline_meters,
    haversine_meters,
    point_in_polygon,
)

_TYPE_SPLIT = re.compile(r"[,/;+]|\band\b")
_TYPE_LABELS = {
    "hide": "hide",
    "hide site": "hide",
    "launch": "launch",
    "launch site": "launch",
    "transload": "transload",
    "transload site": "transload",
    "trans load": "transload",
}


@dataclass(frozen=True)
class Evidence:
    """One reason a site's confidence or a site type changed.

    ``weight`` is added into confidence. ``site_type`` is empty for
    untyped evidence and is ``hide``, ``launch``, or ``transload`` when
    the observation supports that type. Durable kinds (``import``,
    ``declared_type``, ``discovered``) survive a reassessment. Activity
    kinds are replaced each time confidence is recomputed.
    """

    kind: str
    weight: float
    detail: str
    site_type: str = ""
    observed_at: datetime | None = None
    source: str = ""
    layer: str = ""
    message_id: str = ""


@dataclass(frozen=True)
class Site:
    """A place that may be a hide, a launch, a transload, or several.

    Player Data files are points. ``site_types`` is empty when the file
    does not classify the place. A 5-digit numeric name is the MOA
    (first two digits) plus a unique id (last three).

    ``status`` is ``possible``, ``probable``, or ``confirmed``.
    ``confidence`` is the sum of the current evidence, clamped by the
    confidence policy. ``origin`` is ``import`` or ``discovered``.
    """

    name: str
    site_types: frozenset[str] = frozenset()
    status: str = "possible"
    confidence: float = 0.0
    evidence: tuple[Evidence, ...] = ()
    origin: str = ""
    latitude: float | None = None
    longitude: float | None = None
    polygon: tuple[tuple[float, float], ...] = ()
    moa: str = ""
    complex_name: str = ""
    mgrs: str = ""


@dataclass(frozen=True)
class Msr:
    """A main supply route polyline. Points are (latitude, longitude)."""

    name: str
    points: tuple[tuple[float, float], ...]
    length_text: str = ""


@dataclass(frozen=True)
class ReferenceData:
    sites: tuple[Site, ...] = ()
    msrs: tuple[Msr, ...] = ()

    def merge(self, other: "ReferenceData") -> "ReferenceData":
        return ReferenceData(
            sites=self.sites + other.sites,
            msrs=self.msrs + other.msrs,
        )


def match_site(
    latitude: float,
    longitude: float,
    sites: Sequence[Site],
    max_meters: float,
) -> Site | None:
    """Return the closest site containing the point or within max_meters."""

    best: Site | None = None
    best_distance = max_meters
    for site in sites:
        if site.polygon and point_in_polygon(latitude, longitude, site.polygon):
            return site
        if site.latitude is None or site.longitude is None:
            continue
        distance = haversine_meters(
            latitude, longitude, site.latitude, site.longitude
        )
        if distance <= best_distance:
            best = site
            best_distance = distance
    return best


def match_msr(
    latitude: float,
    longitude: float,
    msrs: Sequence[Msr],
    corridor_meters: float,
) -> Msr | None:
    """Return the route whose corridor contains the point, if any."""

    best: Msr | None = None
    best_distance = corridor_meters
    for route in msrs:
        distance = distance_to_polyline_meters(latitude, longitude, route.points)
        if distance <= best_distance:
            best = route
            best_distance = distance
    return best


def parse_site_types(raw: str) -> frozenset[str]:
    """Map a file label onto a set of hide, launch, and transload.

    Separators are comma, slash, semicolon, plus, and the word ``and``.
    ``hide/launch`` and ``transload and hide`` are combinations. An
    unrecognized token is ignored. Player Data with no type column
    yields an empty set.
    """

    text = raw.strip().lower().replace("-", " ").replace("_", " ")
    found: set[str] = set()
    for part in _TYPE_SPLIT.split(text):
        label = " ".join(part.split())
        mapped = _TYPE_LABELS.get(label)
        if mapped is not None:
            found.add(mapped)
    return frozenset(found)
