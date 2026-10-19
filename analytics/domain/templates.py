"""Coord-card message templates.

A template names a detection regex, a separator (``lines``, ``comma``,
or ``slashes``), ordered fields with types, and an evidence mapping
(``tasking`` or ``report``). ADHOC and SITREP are examples shipped on
the sample card, not hardcoded formats. Unknown field types are kept
as attributes so another exercise can add a template without a code
change.

Classification banners are stored and are not treated as fields. Info
logs name the template and its id. The raw body is debug only.
``-`` and blank fields are null. Zero-width characters are removed.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from analytics.domain.coordcard import CoordCard, MgrsGrid
from analytics.domain.correlation import TacrepMessage
from analytics.domain.positions import (
    missing_text,
    parse_location,
    parse_report_time,
    strip_invisible,
)
from analytics.domain.rooms import TypeRooms

logger = logging.getLogger(__name__)

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
_MARKER_ID = re.compile(r"(\d{2}[A-Za-z]{3}\d{4})")
_NUMBERED = re.compile(r"^(\d+)\.\s*(.*)$")
_MGRS = re.compile(r"^\d{1,2}[C-HJ-NP-X][A-HJ-NP-Z]{2}\d{2,10}$", re.IGNORECASE)
_FREQ = re.compile(r"\d+(?:\.\d+)?\s*(?:mhz|khz|hz|ghz)\b", re.IGNORECASE)
_HEADING = re.compile(r"^\d{1,3}(?:\s*deg(?:rees)?)?$", re.IGNORECASE)
_AT = re.compile(r"^(?P<equip>.+?)\s+at\s+(?P<place>\S+)$", re.IGNORECASE)
_VACANT = re.compile(r"\bVACANT\b", re.IGNORECASE)
_TAI = re.compile(r"\(TAI\)\s*(.+?)\s*(?:-\s*VACANT|\bVACANT\b|$)", re.IGNORECASE)
_ROOM = re.compile(r"\bin\s+(.+?)\s*\(", re.IGNORECASE)
_DOMAIN = re.compile(r"\(([^)]*?)\s*Chatsurfer\)", re.IGNORECASE)
_SPECIFIC = ("time", "frequency", "activity", "equipment_at", "location", "heading")


class TemplateError(ValueError):
    """The template section cannot be read."""


@dataclass(frozen=True)
class TemplateField:
    """One ordered field. ``line`` selects a numbered ADHOC line."""

    name: str
    type: str
    pattern: str = ""
    line: int | None = None


@dataclass(frozen=True)
class MessageTemplate:
    """One report shape declared by the coord card."""

    name: str
    detect: str
    separator: str
    fields: tuple[TemplateField, ...]
    evidence: str = "report"
    classification: bool = False
    rooms: TypeRooms | None = None


@dataclass(frozen=True)
class Tasking:
    """One collection requirement, usually an ADHOC."""

    task_id: str
    name: str
    classification: str
    priority: int | None
    effects: tuple[str, ...]
    assets: tuple[str, ...]
    target_name: str
    target_id: str
    latitude: float | None
    longitude: float | None
    elevation: str
    equipment: tuple[str, ...]
    ltiov: str
    reporting: str
    tai: str
    report_room: str
    vacant: bool
    observed_at: datetime
    warning: str
    source_id: str
    raw: str
    location_id: str = ""
    attributes: tuple[tuple[str, str], ...] = ()
    chat_message_id: str = ""


@dataclass(frozen=True)
class ExerciseText:
    """Messages and taskings found in one chat body."""

    messages: tuple[TacrepMessage, ...] = ()
    taskings: tuple[Tasking, ...] = ()


@dataclass
class _Draft:
    values: dict[str, str] = field(default_factory=dict)
    attributes: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    classification: str = ""
    priority: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    effects: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    equipment: tuple[str, ...] = ()
    target_name: str = ""
    target_id: str = ""
    location_id: str = ""
    tai: str = ""
    report_room: str = ""
    vacant: bool = False
    marker_ids: list[str] = field(default_factory=list)


def templates_from_mapping(value: object) -> tuple[MessageTemplate, ...]:
    """Read the card's ``templates`` list."""

    if not isinstance(value, list):
        raise TemplateError("templates must be a list.")
    found: list[MessageTemplate] = []
    for item in value:
        if not isinstance(item, dict):
            raise TemplateError("Each template must be a mapping.")
        found.append(_template(item))
    return tuple(found)


def parse_templated(
    text: str,
    card: CoordCard,
    *,
    source_id: str,
    observed_at: datetime | None,
    grid: MgrsGrid | None,
) -> ExerciseText | None:
    """Parse one body when a template matches. None means try TACREP next."""

    if not card.templates:
        return None
    cleaned = strip_invisible(text).strip()
    if not cleaned:
        return None
    for template in card.templates:
        if not isinstance(template, MessageTemplate) or not template.detect:
            continue
        if re.search(template.detect, cleaned, flags=re.IGNORECASE | re.MULTILINE) is None:
            continue
        return _parse_one(
            cleaned,
            template,
            source_id=source_id,
            observed_at=observed_at,
            grid=grid,
        )
    return None


def link_responses(
    messages: Sequence[TacrepMessage],
    taskings: Sequence[Tasking],
    *,
    match_meters: float,
) -> tuple[TacrepMessage, ...]:
    """Mark reports that answer a tasking by place, TAI, equipment, or day."""

    if not taskings:
        return tuple(messages)
    linked: list[TacrepMessage] = []
    for message in messages:
        if message.tacrep_type.upper() == "ADHOC":
            linked.append(message)
            continue
        hits = [
            task.task_id
            for task in taskings
            if task.task_id and _responds(message, task, match_meters)
        ]
        if not hits:
            linked.append(message)
            continue
        attributes = tuple(
            item for item in message.attributes if item[0] != "responds_to"
        ) + tuple(("responds_to", task_id) for task_id in hits)
        linked.append(
            replace_message(message, attributes=attributes)
        )
    return tuple(linked)


def replace_message(message: TacrepMessage, **changes: object) -> TacrepMessage:
    """Copy a message. Isolated so tests can patch one field."""

    from dataclasses import replace

    updated = replace(message, **changes)  # type: ignore[arg-type]
    if not isinstance(updated, TacrepMessage):
        raise TypeError("replace did not return a TACREP message")
    return updated


def _template(item: dict[str, object]) -> MessageTemplate:
    name = str(item.get("name", "") or "").strip()
    if not name:
        raise TemplateError("A template needs a name.")
    detect = str(item.get("detect", item.get("marker", "")) or "").strip()
    if not detect:
        raise TemplateError(f"{name} needs a detect regex.")
    try:
        re.compile(detect)
    except re.error as exc:
        raise TemplateError(f"{name} detect regex is invalid.") from exc
    separator = str(item.get("separator", "lines") or "lines").strip().lower()
    if separator not in {"lines", "comma", "slashes"}:
        raise TemplateError(f"{name} separator must be lines, comma, or slashes.")
    evidence = str(item.get("evidence", "report") or "report").strip().lower()
    if evidence not in {"tasking", "report"}:
        raise TemplateError(f"{name} evidence must be tasking or report.")
    raw_fields = item.get("fields", ())
    if not isinstance(raw_fields, list) or not raw_fields:
        raise TemplateError(f"{name} needs an ordered fields list.")
    fields = tuple(_field(name, entry) for entry in raw_fields)
    rooms = _template_rooms(item.get("rooms"))
    classification = bool(item.get("classification", False))
    return MessageTemplate(
        name=name,
        detect=detect,
        separator=separator,
        fields=fields,
        evidence=evidence,
        classification=classification,
        rooms=rooms,
    )


def _field(template: str, entry: object) -> TemplateField:
    if isinstance(entry, str):
        return TemplateField(name=entry.strip().lower(), type="text")
    if not isinstance(entry, dict):
        raise TemplateError(f"{template} field must be a name or a mapping.")
    name = str(entry.get("name", "") or "").strip().lower()
    if not name:
        raise TemplateError(f"{template} field needs a name.")
    kind = str(entry.get("type", "text") or "text").strip().lower()
    pattern = str(entry.get("pattern", "") or "")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise TemplateError(f"{template} field {name} has an invalid pattern.") from exc
    line = entry.get("line")
    number = None
    if line not in (None, ""):
        if isinstance(line, bool) or not isinstance(line, (int, float, str)):
            raise TemplateError(f"{template} field {name} line must be an integer.")
        number = int(line)
    return TemplateField(name=name, type=kind, pattern=pattern, line=number)


def _template_rooms(value: object) -> TypeRooms | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TemplateError("template rooms must be a mapping.")
    ingest = _room_side(value, "ingest")
    publish = _room_side(value, "publish")
    return TypeRooms(ingest=ingest, publish=publish)


def _room_side(value: dict[str, object], key: str) -> tuple[str, ...] | None:
    if key not in value:
        return None
    raw = value.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = raw.replace("|", ",").replace(";", ",").split(",")
    elif isinstance(raw, list):
        parts = [str(item) for item in raw]
    else:
        raise TemplateError(f"template {key} rooms must be a list.")
    rooms: list[str] = []
    for part in parts:
        name = part.strip()
        if name and name != "-" and name not in rooms:
            rooms.append(name)
    return tuple(rooms)


def _parse_one(
    text: str,
    template: MessageTemplate,
    *,
    source_id: str,
    observed_at: datetime | None,
    grid: MgrsGrid | None,
) -> ExerciseText:
    moment = observed_at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    draft = _Draft()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    body_lines = _peel_banners(lines, draft) if template.classification else lines
    if template.separator == "comma":
        _parse_comma(body_lines, template, draft, moment, grid)
    elif template.separator == "slashes":
        _parse_slashes(body_lines, template, draft, moment, grid)
    else:
        _parse_lines(body_lines, template, draft, moment, grid)
    ident = _identity(draft, template)
    if template.classification:
        logger.info("Parsed %s %s", template.name, ident)
        logger.debug("template body %s", text)
    else:
        logger.info("Parsed %s %s", template.name, ident)
        logger.debug("template body %s", text)
    warning = "; ".join(draft.warnings)
    if template.evidence == "tasking":
        task = _tasking(draft, template, ident, moment, source_id, text, warning)
        message = _message_from_task(task, template)
        messages = () if message is None else (message,)
        return ExerciseText(messages=messages, taskings=(task,))
    message = _report_message(draft, template, ident, moment, source_id, text, warning)
    if message is None:
        return ExerciseText()
    return ExerciseText(messages=(message,))


def _peel_banners(lines: list[str], draft: _Draft) -> list[str]:
    if len(lines) < 2:
        return lines
    first, last = lines[0], lines[-1]
    if _is_banner(first) and _is_banner(last):
        draft.classification = first if first.upper() == last.upper() else f"{first} / {last}"
        draft.attributes.append(("classification", draft.classification))
        return lines[1:-1]
    return lines


def _is_banner(line: str) -> bool:
    if re.search(r"\*\*\*(?:START|END)\b", line, re.IGNORECASE):
        return False
    if re.match(r"^(?:PRI:|\d+\.)", line, re.IGNORECASE):
        return False
    if line.upper().startswith("SITREP"):
        return False
    letters = re.sub(r"[^A-Za-z]", "", line)
    return bool(letters) and letters.upper() == letters and len(line) <= 80


def _parse_lines(
    lines: list[str],
    template: MessageTemplate,
    draft: _Draft,
    observed_at: datetime,
    grid: MgrsGrid | None,
) -> None:
    numbered: dict[int, str] = {}
    for line in lines:
        for item in template.fields:
            if item.pattern and re.search(item.pattern, line, flags=re.IGNORECASE):
                _apply(item, line, draft, observed_at, grid)
        match = _NUMBERED.match(line)
        if match is not None:
            numbered[int(match.group(1))] = match.group(2).strip()
        elif re.match(r"(?i)^PRI\s*:", line):
            _apply(TemplateField("priority", "integer"), line, draft, observed_at, grid)
        else:
            _capture_marker(line, draft)
    content = [item for item in template.fields if item.type not in {"banner", "marker"}]
    for item in content:
        if item.line is None or item.line not in numbered:
            continue
        _apply(item, numbered[item.line], draft, observed_at, grid)
    _check_markers(draft)


def _parse_comma(
    lines: list[str],
    template: MessageTemplate,
    draft: _Draft,
    observed_at: datetime,
    grid: MgrsGrid | None,
) -> None:
    body = " ".join(lines)
    body = re.sub(rf"(?i)^{re.escape(template.name)}\s*:\s*", "", body).strip()
    tokens = [part.strip() for part in body.split(",")]
    fields = [item for item in template.fields if item.type != "banner"]
    specific = [item for item in fields if item.type in _SPECIFIC]
    generic = [item for item in fields if item.type not in _SPECIFIC]
    for token in tokens:
        if missing_text(token):
            continue
        if _place(specific, token, draft, observed_at, grid):
            continue
        _place(generic, token, draft, observed_at, grid, accept_any=True)


def _parse_slashes(
    lines: list[str],
    template: MessageTemplate,
    draft: _Draft,
    observed_at: datetime,
    grid: MgrsGrid | None,
) -> None:
    parts = [part.strip() for part in re.split(r"\s*//\s*", " ".join(lines))]
    fields = [item for item in template.fields if item.type != "banner"]
    for item, token in zip(fields, parts, strict=False):
        if missing_text(token):
            continue
        _apply(item, token, draft, observed_at, grid)


def _place(
    fields: list[TemplateField],
    token: str,
    draft: _Draft,
    observed_at: datetime,
    grid: MgrsGrid | None,
    *,
    accept_any: bool = False,
) -> bool:
    for item in fields:
        if item.name in draft.values:
            continue
        if accept_any or _accepts(item.type, token, grid):
            _apply(item, token, draft, observed_at, grid)
            return True
    return False


def _accepts(kind: str, token: str, grid: MgrsGrid | None) -> bool:
    if kind == "time":
        return bool(re.search(r"\d{4,6}\s*Z", token, re.IGNORECASE))
    if kind == "frequency":
        return _FREQ.search(token) is not None or token.upper().startswith("FREQ")
    if kind == "activity":
        return token.strip().upper() in {"ACTIVE", "AWAKE"}
    if kind == "equipment_at":
        return _AT.match(token.strip()) is not None
    if kind == "location":
        compact = token.replace(" ", "")
        return _MGRS.match(compact) is not None or parse_location(token, grid) is not None
    if kind == "heading":
        return _HEADING.match(token.strip()) is not None and "Z" not in token.upper()
    return False


def _apply(
    item: TemplateField,
    text: str,
    draft: _Draft,
    observed_at: datetime,
    grid: MgrsGrid | None,
) -> None:
    if missing_text(text):
        return
    kind = item.type
    if kind == "marker":
        _capture_marker(text, draft)
        return
    if kind == "integer":
        match = re.search(r"(\d+)", text)
        if match is not None:
            draft.priority = int(match.group(1))
            draft.values[item.name] = match.group(1)
        return
    if kind == "effect_lists":
        _effects(text, draft, item.name)
        return
    if kind == "effects":
        draft.effects = _csv_items(_after_colon(text))
        draft.values[item.name] = ", ".join(draft.effects)
        return
    if kind == "assets":
        draft.assets = _csv_items(_after_colon(text))
        draft.values[item.name] = ", ".join(draft.assets)
        return
    if kind == "name_id":
        _name_id(text, draft, item.name)
        return
    if kind == "location":
        _location(text, draft, item.name, grid)
        return
    if kind == "equipment":
        draft.equipment = _csv_items(_after_colon(text))
        draft.values[item.name] = ", ".join(draft.equipment)
        return
    if kind == "equipment_at":
        _equipment_at(text, draft, item.name)
        return
    if kind == "reporting":
        _reporting(text, draft, item.name)
        return
    if kind == "time":
        draft.values[item.name] = text.strip()
        return
    if kind in {"frequency", "heading", "activity", "text", "elevation"}:
        draft.values[item.name] = _after_colon(text)
        if kind not in {"text", "elevation"}:
            draft.attributes.append((item.name, draft.values[item.name]))
        return
    draft.values[item.name] = text.strip()
    draft.attributes.append((item.name, text.strip()))


def _capture_marker(text: str, draft: _Draft) -> None:
    match = _MARKER_ID.search(text.replace(" ", ""))
    if match is not None and re.search(r"ADHOC", text, re.IGNORECASE):
        draft.marker_ids.append(match.group(1).upper())


def _check_markers(draft: _Draft) -> None:
    ids = list(dict.fromkeys(draft.marker_ids))
    if len(ids) > 1:
        draft.warnings.append(f"START/END ids differ: {', '.join(ids)}")


def _effects(text: str, draft: _Draft, name: str) -> None:
    body = _after_colon(text)
    parts = re.split(r"(?i)\bassets?\b\s*:?", body, maxsplit=1)
    draft.effects = _csv_items(parts[0])
    draft.assets = _csv_items(parts[1]) if len(parts) > 1 else ()
    draft.values[name] = body


def _name_id(text: str, draft: _Draft, name: str) -> None:
    body = _after_colon(text)
    pieces = [part.strip() for part in body.split(",") if part.strip() and part.strip() != "-"]
    draft.target_name = pieces[0] if pieces else ""
    draft.target_id = pieces[1] if len(pieces) > 1 else ""
    draft.values[name] = body
    if draft.target_name:
        draft.attributes.append(("target_name", draft.target_name))
    if draft.target_id:
        draft.attributes.append(("target_id", draft.target_id))


def _location(text: str, draft: _Draft, name: str, grid: MgrsGrid | None) -> None:
    body = _after_colon(text)
    left, _, right = body.partition(",")
    point = parse_location(left.strip(), grid)
    elevation = right.strip()
    if point is None:
        point = parse_location(body, grid)
        elevation = ""
    if point is None and grid is not None:
        compact = body.replace(" ", "")
        if _MGRS.match(compact):
            point = grid.to_latlon(compact)
            draft.values["mgrs"] = compact
    if point is not None:
        draft.latitude, draft.longitude = point
    if elevation and elevation != "-":
        draft.values["elevation"] = elevation
        draft.attributes.append(("elevation", elevation))
    draft.values[name] = body


def _equipment_at(text: str, draft: _Draft, name: str) -> None:
    match = _AT.match(text.strip())
    if match is None:
        draft.values[name] = text.strip()
        draft.equipment = (text.strip(),)
        return
    equip = match.group("equip").strip()
    place = match.group("place").strip(" .")
    draft.equipment = (equip,)
    draft.location_id = place
    draft.values[name] = text.strip()
    draft.attributes.append(("equipment", equip))
    draft.attributes.append(("location_id", place))


def _reporting(text: str, draft: _Draft, name: str) -> None:
    body = _after_colon(text)
    draft.values[name] = body
    draft.vacant = _VACANT.search(body) is not None
    tai = _TAI.search(body)
    if tai is not None:
        draft.tai = tai.group(1).strip(" -")
    room = _ROOM.search(body)
    if room is not None:
        draft.report_room = room.group(1).strip()
    if draft.tai:
        draft.attributes.append(("tai", draft.tai))
    if draft.report_room:
        draft.attributes.append(("report_room", draft.report_room))
    if draft.vacant:
        draft.attributes.append(("vacant", "true"))
    domain = _DOMAIN.search(body)
    if domain is not None and domain.group(1).strip():
        draft.attributes.append(("domain", domain.group(1).strip()))


def _after_colon(text: str) -> str:
    if ":" in text:
        return text.split(":", 1)[1].strip()
    return text.strip()


def _csv_items(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for part in text.split(","):
        token = part.strip(" .")
        if token and token != "-" and token.upper() not in {"ASSET", "ASSETS"}:
            found.append(token)
    return tuple(found)


def _identity(draft: _Draft, template: MessageTemplate) -> str:
    if draft.marker_ids:
        return draft.marker_ids[0]
    callsign = draft.values.get("callsign", "")
    clock = draft.values.get("time", "")
    if callsign or clock:
        return f"{template.name}:{callsign}:{clock}".strip(":")
    return template.name


def _stamp(token: str, fallback: datetime) -> datetime:
    if len(token) < 7:
        return fallback
    month = _MONTHS.get(token[2:5].upper())
    if month is None:
        return fallback
    try:
        return datetime(2000 + int(token[5:7]), month, int(token[0:2]), tzinfo=UTC)
    except ValueError:
        return fallback


def _tasking(
    draft: _Draft,
    template: MessageTemplate,
    ident: str,
    moment: datetime,
    source_id: str,
    raw: str,
    warning: str,
) -> Tasking:
    observed = _stamp(ident, moment) if draft.marker_ids else moment
    timed = draft.values.get("time", "")
    if timed:
        parsed = parse_report_time(timed, observed)
        if parsed is not None:
            observed = parsed
    return Tasking(
        task_id=ident,
        name=template.name,
        classification=draft.classification,
        priority=draft.priority,
        effects=draft.effects,
        assets=draft.assets,
        target_name=draft.target_name,
        target_id=draft.target_id,
        latitude=draft.latitude,
        longitude=draft.longitude,
        elevation=draft.values.get("elevation", ""),
        equipment=draft.equipment,
        ltiov=draft.values.get("ltiov") or draft.values.get("reporting", ""),
        reporting=draft.values.get("reporting", ""),
        tai=draft.tai,
        report_room=draft.report_room,
        vacant=draft.vacant,
        observed_at=observed,
        warning=warning,
        source_id=source_id,
        raw=raw,
        location_id=draft.location_id,
        attributes=tuple(draft.attributes),
    )


def _message_from_task(task: Tasking, template: MessageTemplate) -> TacrepMessage | None:
    if task.latitude is None or task.longitude is None:
        return None
    return TacrepMessage(
        kind="TASK",
        sequence=task.task_id,
        observed_at=task.observed_at,
        latitude=task.latitude,
        longitude=task.longitude,
        source_id=task.source_id,
        source_message_id=task.task_id,
        subject=task.target_name,
        bmoa=task.tai,
        raw=task.raw,
        serial_base=task.task_id,
        serial_full=task.task_id,
        correction_rank=0,
        tacrep_type=template.name,
        block_warning=task.warning,
        attributes=task.attributes,
        equipment=task.equipment,
        report_room=task.report_room,
        classification=task.classification,
        vacant=task.vacant,
    )


def _report_message(
    draft: _Draft,
    template: MessageTemplate,
    ident: str,
    moment: datetime,
    source_id: str,
    raw: str,
    warning: str,
) -> TacrepMessage | None:
    if draft.latitude is None or draft.longitude is None:
        return None
    timed = parse_report_time(draft.values.get("time", ""), moment)
    activity = draft.values.get("activity", "")
    events = (activity.lower(),) if activity.lower() in {"active", "awake"} else ()
    return TacrepMessage(
        kind="S",
        sequence=ident,
        observed_at=timed or moment,
        latitude=draft.latitude,
        longitude=draft.longitude,
        source_id=source_id,
        source_message_id=ident,
        subject=draft.values.get("callsign", ""),
        callsign=draft.values.get("callsign", ""),
        heading=_heading(draft.values.get("heading", "")),
        raw=raw,
        serial_base=ident,
        serial_full=ident,
        correction_rank=0,
        tacrep_type=template.name,
        block_warning=warning,
        attributes=tuple(draft.attributes),
        events=events,
        equipment=draft.equipment,
        bmoa=draft.location_id,
        classification=draft.classification,
    )


def _heading(text: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        return None
    return float(match.group(0)) % 360


def _responds(message: TacrepMessage, task: Tasking, match_meters: float) -> bool:
    if task.tai and message.bmoa and _same(task.tai, message.bmoa):
        return True
    if task.equipment and message.equipment and _overlap(task.equipment, message.equipment):
        return True
    if task.location_id and _same(task.location_id, message.bmoa):
        return True
    if (
        task.latitude is not None
        and task.longitude is not None
        and _distance(message, task) <= match_meters
    ):
        return True
    if task.observed_at.date() == message.observed_at.date() and (
        task.tai or task.equipment or task.latitude is not None
    ):
        if task.equipment and message.equipment and _overlap(task.equipment, message.equipment):
            return True
        if task.tai and message.bmoa and _same(task.tai, message.bmoa):
            return True
    return False


def _same(left: str, right: str) -> bool:
    return left.strip().upper() == right.strip().upper()


def _overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    have = {item.strip().upper() for item in left if item.strip()}
    return any(item.strip().upper() in have for item in right)


def _distance(message: TacrepMessage, task: Tasking) -> float:
    from analytics.domain.geo import haversine_meters

    assert task.latitude is not None and task.longitude is not None
    return haversine_meters(
        message.latitude,
        message.longitude,
        task.latitude,
        task.longitude,
    )
