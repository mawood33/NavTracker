"""Non-GPS reports attached to sites, hex cells, and MSR corridors.

TACREP parsing is provisional. https://github.com/mawood33/TransloadOracle
returned 404 from this environment, so this module is not a port of that
repository. ``parse_tacrep`` and ``ReportPolicy`` are the functions to
replace when that repository can be read.

A ChatSurfer TACREP room accepts either JSON or a short text form.

JSON keys: ``type`` / ``activity`` / ``category``, ``latitude`` or
``lat``, ``longitude`` or ``lon``, ``timestamp`` / ``time`` / ``dtg``,
``id``, and ``text`` / ``remarks`` / ``summary``. A nested ``location``
object with lat/lon is also accepted.

Text form, one field per line::

    TACREP
    DTG: 011200ZJAN2026
    LOC: 10.0 20.0
    ACTIVITY: transload
    REMARKS: trucks transferred cargo

``LOC`` may be decimal degrees or DMS (``10°00'00"N 20°00'00"E``).
A message with no position is kept out of correlation. Keyword lists
on ``ReportPolicy`` map the text onto hide, launch, or transload.
Spatial relevance is proximity to a site, the report's hex, or an MSR
corridor, inside ``window_seconds``.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from analytics.domain.geo import haversine_meters, point_in_polygon
from analytics.domain.reference import Msr, match_msr, parse_site_types
from analytics.domain.thresholds import AnalysisThresholds

_DTG_RE = re.compile(
    r"\b(\d{2})(\d{2})(\d{2})Z([A-Za-z]{3})(\d{2}|\d{4})\b",
    re.IGNORECASE,
)
_DMS_RE = re.compile(
    r"(\d+)\s*°\s*(\d+)\s*['′]\s*([\d.]+)\s*[\"″]?\s*([NnSsEeWw])"
)
_LOC_RE = re.compile(
    r"(?:LOC|POS|LOCATION)\s*[:=]\s*(.+)",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(r"^(DTG|ACTIVITY|TYPE|REMARKS|SUMMARY)\s*[:=]\s*(.*)$", re.IGNORECASE)
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass(frozen=True)
class ReportPolicy:
    """Weights and keywords for report evidence. Decay is not applied here.

    ``weight`` is added once per correlated report. ``type_weight`` is
    added again when the report supports one site type. The sum of both
    across the reports on one site stops at ``cap``.
    """

    window_seconds: float = 86_400
    weight: float = 0.1
    type_weight: float = 0.12
    cap: float = 0.3
    hide_keywords: tuple[str, ...] = ("hide", "conceal", "cache")
    launch_keywords: tuple[str, ...] = ("launch", "firing", "missile")
    transload_keywords: tuple[str, ...] = (
        "transload",
        "offload",
        "onload",
        "transfer",
    )


@dataclass(frozen=True)
class Report:
    """One external observation. ``layer`` is a room name or a Clairvoyant layer."""

    source: str
    layer: str
    message_id: str
    observed_at: datetime
    latitude: float | None
    longitude: float | None
    site_type: str
    summary: str
    keywords: tuple[str, ...] = ()


def report_key(report: Report) -> tuple[str, str, str]:
    """Identity used to drop the same message seen twice."""

    if report.message_id:
        return (report.source, report.layer, report.message_id)
    latitude = "" if report.latitude is None else f"{report.latitude:.5f}"
    longitude = "" if report.longitude is None else f"{report.longitude:.5f}"
    return (
        report.source,
        report.layer,
        "|".join((report.observed_at.isoformat(), latitude, longitude, report.summary)),
    )


def dedupe_reports(reports: Sequence[Report]) -> tuple[Report, ...]:
    """Keep the first copy of each source, layer, and message id."""

    found: dict[tuple[str, str, str], Report] = {}
    for report in reports:
        found.setdefault(report_key(report), report)
    return tuple(found.values())


def parse_tacrep(
    body: str,
    *,
    layer: str,
    message_id: str,
    received_at: datetime | None,
    policy: ReportPolicy | None = None,
) -> Report | None:
    """Parse one TACREP room body. Returns None when it has no usable position."""

    rules = policy or ReportPolicy()
    text = body.strip()
    if not text:
        return None
    if text[0] in "{[":
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            return _from_mapping(
                payload,
                source="chatsurfer",
                layer=layer,
                message_id=message_id,
                received_at=received_at,
                policy=rules,
            )
    if "tacrep" not in text.lower() and _LOC_RE.search(text) is None:
        return None
    return _from_text(
        text,
        source="chatsurfer",
        layer=layer,
        message_id=message_id,
        received_at=received_at,
        policy=rules,
    )


def parse_layer_payload(
    body: object,
    *,
    layer: str,
    policy: ReportPolicy | None = None,
) -> list[Report]:
    """Parse a Clairvoyant layer body. Unknown shapes yield no reports."""

    rules = policy or ReportPolicy()
    if isinstance(body, list):
        entries: list[object] = body
    elif isinstance(body, dict) and isinstance(body.get("reports"), list):
        entries = body["reports"]
    elif isinstance(body, dict):
        entries = [body]
    else:
        return []
    found: list[Report] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message_id = str(entry.get("id", entry.get("message_id", "")) or "")
        report = _from_mapping(
            entry,
            source="clairvoyant",
            layer=layer,
            message_id=message_id,
            received_at=None,
            policy=rules,
        )
        if report is not None:
            found.append(report)
    return found


def select_reports(
    reports: Sequence[Report],
    *,
    latitude: float | None,
    longitude: float | None,
    polygon: tuple[tuple[float, float], ...],
    as_of: datetime,
    policy: ReportPolicy,
    max_meters: float,
) -> tuple[Report, ...]:
    """Reports inside the time window and the site match distance."""

    found: list[Report] = []
    for report in reports:
        if not _in_window(report, as_of, policy):
            continue
        assert report.latitude is not None and report.longitude is not None
        if polygon and point_in_polygon(report.latitude, report.longitude, polygon):
            found.append(report)
            continue
        if latitude is None or longitude is None:
            continue
        distance = haversine_meters(
            report.latitude, report.longitude, latitude, longitude
        )
        if distance <= max_meters:
            found.append(report)
    return tuple(found)


def select_route_reports(
    route: Msr,
    reports: Sequence[Report],
    as_of: datetime,
    policy: ReportPolicy,
    thresholds: AnalysisThresholds,
) -> tuple[Report, ...]:
    """Reports whose point lies in the route corridor during the window."""

    found: list[Report] = []
    for report in reports:
        if not _in_window(report, as_of, policy):
            continue
        assert report.latitude is not None and report.longitude is not None
        if (
            match_msr(
                report.latitude,
                report.longitude,
                (route,),
                thresholds.msr_corridor_meters,
            )
            is not None
        ):
            found.append(report)
    return tuple(found)


def reports_by_cell(
    reports: Sequence[Report],
    as_of: datetime,
    policy: ReportPolicy,
    cell_id_for: Callable[[float, float], str],
) -> dict[str, tuple[Report, ...]]:
    """Group in-window reports by hex id. ``cell_id_for`` hides H3."""

    grouped: dict[str, list[Report]] = {}
    for report in reports:
        if not _in_window(report, as_of, policy):
            continue
        assert report.latitude is not None and report.longitude is not None
        cell_id = cell_id_for(report.latitude, report.longitude)
        if cell_id:
            grouped.setdefault(cell_id, []).append(report)
    return {key: tuple(value) for key, value in grouped.items()}


def report_document(report: Report) -> dict[str, object]:
    return {
        "source": report.source,
        "layer": report.layer,
        "message_id": report.message_id,
        "observed_at": report.observed_at.isoformat(),
        "latitude": report.latitude,
        "longitude": report.longitude,
        "site_type": report.site_type,
        "summary": report.summary,
        "keywords": list(report.keywords),
    }


def _in_window(report: Report, as_of: datetime, policy: ReportPolicy) -> bool:
    if report.latitude is None or report.longitude is None:
        return False
    start = as_of - timedelta(seconds=policy.window_seconds)
    return start <= report.observed_at <= as_of


def _from_mapping(
    payload: Mapping[str, object],
    *,
    source: str,
    layer: str,
    message_id: str,
    received_at: datetime | None,
    policy: ReportPolicy,
) -> Report | None:
    latitude, longitude = _coordinates(payload)
    if latitude is None or longitude is None:
        return None
    observed = _time_value(payload) or received_at
    if observed is None:
        return None
    summary = _text_value(payload)
    category = _first_text(payload, ("activity", "category", "site_type"))
    site_type, keywords = _classify(category, summary, policy)
    identity = message_id or _first_text(payload, ("id", "message_id"))
    return Report(
        source=source,
        layer=layer,
        message_id=identity,
        observed_at=observed,
        latitude=latitude,
        longitude=longitude,
        site_type=site_type,
        summary=summary,
        keywords=keywords,
    )


def _from_text(
    text: str,
    *,
    source: str,
    layer: str,
    message_id: str,
    received_at: datetime | None,
    policy: ReportPolicy,
) -> Report | None:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD_RE.match(line.strip())
        if match:
            fields[match.group(1).upper()] = match.group(2).strip()
    observed = _parse_time(fields.get("DTG", "")) or _parse_time(text) or received_at
    latitude, longitude = _line_coordinates(text)
    if latitude is None or longitude is None or observed is None:
        return None
    summary = fields.get("REMARKS") or fields.get("SUMMARY") or text
    category = fields.get("ACTIVITY") or fields.get("TYPE") or ""
    site_type, keywords = _classify(category, summary, policy)
    return Report(
        source=source,
        layer=layer,
        message_id=message_id,
        observed_at=observed,
        latitude=latitude,
        longitude=longitude,
        site_type=site_type,
        summary=summary.strip(),
        keywords=keywords,
    )


def _classify(
    category: str,
    summary: str,
    policy: ReportPolicy,
) -> tuple[str, tuple[str, ...]]:
    declared = set(parse_site_types(category))
    lowered = f"{category} {summary}".lower()
    found: list[str] = []
    groups = (
        ("hide", policy.hide_keywords),
        ("launch", policy.launch_keywords),
        ("transload", policy.transload_keywords),
    )
    for site_type, words in groups:
        for word in words:
            token = word.strip().lower()
            if token and re.search(rf"\b{re.escape(token)}\b", lowered):
                found.append(token)
                declared.add(site_type)
    ordered = tuple(dict.fromkeys(found))
    if len(declared) == 1:
        return next(iter(declared)), ordered
    return "", ordered


def _coordinates(payload: Mapping[str, object]) -> tuple[float | None, float | None]:
    nested = payload.get("location")
    source: Mapping[str, object]
    if isinstance(nested, dict):
        source = nested
    else:
        source = payload
    latitude = _float_value(source, ("latitude", "lat"))
    longitude = _float_value(source, ("longitude", "lon", "lng"))
    return latitude, longitude


def _float_value(payload: Mapping[str, object], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None


def _time_value(payload: Mapping[str, object]) -> datetime | None:
    for name in ("timestamp", "time", "dtg", "observed_at"):
        value = payload.get(name)
        if isinstance(value, str):
            parsed = _parse_time(value)
            if parsed is not None:
                return parsed
    return None


def _text_value(payload: Mapping[str, object]) -> str:
    for name in ("text", "remarks", "summary", "message"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_text(payload: Mapping[str, object], names: tuple[str, ...]) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_time(text: str) -> datetime | None:
    stripped = text.strip()
    if not stripped:
        return None
    iso = stripped.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    match = _DTG_RE.search(stripped.upper())
    if match is None:
        return None
    day, hour, minute, month_name, year_text = match.groups()
    month = _MONTHS.get(month_name.upper())
    if month is None:
        return None
    year = int(year_text)
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, int(day), int(hour), int(minute), tzinfo=UTC)
    except ValueError:
        return None


def _line_coordinates(text: str) -> tuple[float | None, float | None]:
    located = _LOC_RE.search(text)
    raw = located.group(1).strip() if located else text
    dms = list(_DMS_RE.finditer(raw))
    if len(dms) >= 2:
        return _dms_value(dms[0].groups()), _dms_value(dms[1].groups())
    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", raw)
    if len(numbers) < 2:
        return None, None
    latitude = float(numbers[0])
    longitude = float(numbers[1])
    if abs(latitude) > 90 or abs(longitude) > 180:
        return None, None
    return latitude, longitude


def _dms_value(groups: tuple[str, ...]) -> float:
    degrees, minutes, seconds, hemisphere = groups
    value = int(degrees) + int(minutes) / 60 + float(seconds) / 3600
    if hemisphere.upper() in {"S", "W"}:
        value = -value
    return value
