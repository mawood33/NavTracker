"""Exercise coord card: TACREP types, id blocks, and field order.

Block numbers change per exercise. Nothing in this module invents a
range. A card lists each type name, one or more kind letters, an
inclusive serial range, and the ordered fields for that type. ``id``
is required. Missing values on a message are optional. Other field
names stay available for exercises that are not MTI, IMINT, ELINT, or
FUSED.

``ellipse_unit`` is ``nmi`` (the default), ``km``, or ``meters``.
``magnetic_declination_degrees`` is added to magnetic ellipse
orientation (default 0). CAT is a target-location-error category: a
lower number is more accurate, and each category maps to a circular
error radius. The bullseye name and point are a hook only.

An optional ``rooms`` section names ChatSurfer ingest rooms and the
publish routing table. When that section is present it replaces the
env and config values. When it is absent, env and config are used.

Cards are YAML, JSON, or CSV. ``schema_version`` selects a Draft
2020-12 schema from the loaded catalog. A missing version uses that
catalog's default. Unknown fields stay on the mapping.
"""

import csv
import io
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import yaml

from analytics.domain.catce import (
    DEFAULT_CAT_CE_METERS,
    CatTableError,
    parse_cat_ce,
    radius_for,
)
from analytics.domain.confidence_text import (
    DEFAULT_CONFIDENCE_TERMS,
    ConfidenceTableError,
    ConfidenceTerm,
    terms_from_mapping,
)
from analytics.domain.publish import (
    PublishTable,
    PublishTableError,
    table_from_mapping,
)
from analytics.domain.rooms import TypeRooms


class CoordCardError(ValueError):
    """The text is not a card that can replace the one already loaded."""


class MgrsGrid(Protocol):
    """Converts one MGRS string to latitude and longitude."""

    def to_latlon(self, text: str) -> tuple[float, float] | None:
        """Return the point, or None when the string is not MGRS."""


@dataclass(frozen=True)
class CardRooms:
    """Rooms named by a card. ``None`` means that part was not set.

    A set value, including an empty tuple or an empty publish table,
    replaces the env and config value. ``None`` leaves env and config
    in place.
    """

    ingest: tuple[str, ...] | None = None
    publish: PublishTable | None = None


@dataclass(frozen=True)
class CoordType:
    """One TACREP type on the exercise card."""

    name: str
    kind: str
    start: int
    end: int
    fields: tuple[str, ...]
    kinds: tuple[str, ...] = ()
    rooms: TypeRooms | None = None

    def __post_init__(self) -> None:
        if not self.kinds:
            object.__setattr__(self, "kinds", (self.kind,))


@dataclass(frozen=True)
class CoordCard:
    """The loaded card. An empty card classifies no blocks."""

    types: tuple[CoordType, ...] = ()
    mismatch_meters: float = 250.0
    ellipse_unit: str = "nmi"
    magnetic_declination_degrees: float = 0.0
    cat_ce: tuple[tuple[int, float], ...] = DEFAULT_CAT_CE_METERS
    confidence_terms: tuple[ConfidenceTerm, ...] = DEFAULT_CONFIDENCE_TERMS
    rooms: CardRooms | None = None
    bullseye_name: str = ""
    bullseye_latitude: float | None = None
    bullseye_longitude: float | None = None
    templates: tuple[object, ...] = ()
    schema_version: str = ""

    def match(self, kind: str, serial: int) -> tuple[CoordType | None, str, str]:
        """Return the type, block label, and warning. The id is kept."""

        letter = kind.upper()
        configured = [item for item in self.types if letter in item.kinds]
        matching = [item for item in configured if item.start <= serial <= item.end]
        if matching:
            chosen = matching[0]
            return chosen, _label(letter, chosen), ""
        if not configured:
            return None, "", f"unknown TACREP type for kind {letter}"
        label = ", ".join(_label(letter, item) for item in configured)
        warning = f"serial {letter}-{serial:03d} outside configured block {label}"
        if len(configured) == 1:
            return configured[0], label, warning
        return None, label, warning

    def ce_radius_m(self, level: int) -> float | None:
        """Circular-error radius in meters. A lower CAT is a smaller radius."""

        return radius_for(self.cat_ce, level)

    def fused_type(self) -> CoordType | None:
        """The type used for ``UPDATE <id>`` and ``F`` messages."""

        for item in self.types:
            if "F" in item.kinds or item.name.strip().upper() == "FUSED":
                return item
        for item in self.types:
            if "correlates" in item.fields:
                return item
        return None


def parse_coord_card_text(
    text: str,
    *,
    suffix: str = "",
    mismatch_meters: float = 250.0,
    ellipse_unit: str = "nmi",
    magnetic_declination_degrees: float = 0.0,
    cat_ce: tuple[tuple[int, float], ...] = DEFAULT_CAT_CE_METERS,
    confidence_terms: tuple[ConfidenceTerm, ...] = DEFAULT_CONFIDENCE_TERMS,
    content_type: str = "",
    schema: object | None = None,
) -> CoordCard:
    """Parse YAML, JSON, or CSV. Raises ``CoordCardError`` and does not apply."""

    card, _document, _version = read_coord_card(
        text,
        suffix=suffix,
        mismatch_meters=mismatch_meters,
        ellipse_unit=ellipse_unit,
        magnetic_declination_degrees=magnetic_declination_degrees,
        cat_ce=cat_ce,
        confidence_terms=confidence_terms,
        content_type=content_type,
        schema=schema,
    )
    return card


def read_coord_card(
    text: str,
    *,
    suffix: str = "",
    mismatch_meters: float = 250.0,
    ellipse_unit: str = "nmi",
    magnetic_declination_degrees: float = 0.0,
    cat_ce: tuple[tuple[int, float], ...] = DEFAULT_CAT_CE_METERS,
    confidence_terms: tuple[ConfidenceTerm, ...] = DEFAULT_CONFIDENCE_TERMS,
    content_type: str = "",
    schema: object | None = None,
) -> tuple[CoordCard, dict[str, object], str]:
    """Return the card, the mapping that was checked, and the schema version."""

    from analytics.domain.card_schema import (
        SchemaCatalog,
        SchemaCatalogError,
        bundled_catalog,
        normalize_card_document,
        validate_card_document,
    )

    document = _document_from_text(text, suffix=suffix, content_type=content_type)
    document = normalize_card_document(document)
    catalog = schema if isinstance(schema, SchemaCatalog) else bundled_catalog()
    try:
        version = validate_card_document(document, catalog)
    except SchemaCatalogError as exc:
        raise CoordCardError(str(exc)) from exc
    card = card_from_mapping(
        document,
        mismatch_meters,
        ellipse_unit=ellipse_unit,
        magnetic_declination_degrees=magnetic_declination_degrees,
        cat_ce=cat_ce,
        confidence_terms=confidence_terms,
    )
    if card.schema_version != version:
        card = replace(card, schema_version=version)
    return card, document, version


def _document_from_text(text: str, *, suffix: str, content_type: str) -> dict[str, object]:
    body = text.strip()
    if not body:
        raise CoordCardError("Coord card is empty.")
    kind = detect_card_format(body, suffix=suffix, content_type=content_type)
    if kind == "csv":
        return _csv_document(body)
    if kind == "json":
        return _json_document(body)
    return _yaml_document(body)


def detect_card_format(text: str, *, suffix: str = "", content_type: str = "") -> str:
    """Extension, then Content-Type, then a sniff of the body."""

    ext = Path(suffix).suffix.lower()
    if ext == ".json":
        return "json"
    if ext == ".csv":
        return "csv"
    if ext in {".yaml", ".yml"}:
        return "yaml"
    media = content_type.split(";", 1)[0].strip().lower()
    if media in {"application/json", "text/json"}:
        return "json"
    if media in {"text/csv", "application/csv"}:
        return "csv"
    if media in {"application/yaml", "text/yaml", "application/x-yaml", "text/x-yaml"}:
        return "yaml"
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    if _looks_like_csv(text):
        return "csv"
    return "yaml"


def _json_document(body: str) -> dict[str, object]:
    import json

    try:
        loaded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CoordCardError("Coord card is not valid JSON.") from exc
    if not isinstance(loaded, dict):
        raise CoordCardError("Coord card must be a mapping.")
    return loaded


def _yaml_document(body: str) -> dict[str, object]:
    try:
        loaded = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise CoordCardError("Coord card is not valid YAML or CSV.") from exc
    if not isinstance(loaded, dict):
        raise CoordCardError("Coord card must be a mapping.")
    return loaded


def card_from_mapping(
    payload: dict[str, object],
    mismatch_meters: float,
    *,
    ellipse_unit: str = "nmi",
    magnetic_declination_degrees: float = 0.0,
    cat_ce: tuple[tuple[int, float], ...] = DEFAULT_CAT_CE_METERS,
    confidence_terms: tuple[ConfidenceTerm, ...] = DEFAULT_CONFIDENCE_TERMS,
) -> CoordCard:
    """Validate a decoded card. Overlapping blocks for one letter fail."""

    raw_types = payload.get("types", payload.get("tacreps"))
    if not isinstance(raw_types, list) or not raw_types:
        raise CoordCardError("Coord card needs a types list.")
    mismatch = payload.get("mismatch_meters", mismatch_meters)
    distance = _number("mismatch_meters", mismatch)
    if distance < 0:
        raise CoordCardError("mismatch_meters must be >= 0.")
    unit = _ellipse_unit(payload.get("ellipse_unit", ellipse_unit))
    declination = _number(
        "magnetic_declination_degrees",
        payload.get("magnetic_declination_degrees", magnetic_declination_degrees),
    )
    radii = cat_ce
    if "cat_ce" in payload:
        try:
            radii = parse_cat_ce(payload.get("cat_ce"))
        except CatTableError as exc:
            raise CoordCardError(str(exc)) from exc
    terms = confidence_terms
    if "confidence_terms" in payload:
        try:
            terms = terms_from_mapping(payload.get("confidence_terms"))
        except ConfidenceTableError as exc:
            raise CoordCardError(str(exc)) from exc
    rooms = None
    if "rooms" in payload:
        rooms = _rooms(payload.get("rooms"))
    templates: tuple[object, ...] = ()
    if "templates" in payload:
        templates = _templates(payload.get("templates"))
    bullseye_name, bullseye_lat, bullseye_lon = _bullseye(payload.get("bullseye"))
    schema_version = _schema_version(payload.get("schema_version", ""))
    types: list[CoordType] = []
    for entry in raw_types:
        if not isinstance(entry, dict):
            raise CoordCardError("Each coord card type must be a mapping.")
        types.append(_type_from_mapping(entry))
    _reject_overlaps(types)
    return CoordCard(
        types=tuple(types),
        mismatch_meters=distance,
        ellipse_unit=unit,
        magnetic_declination_degrees=declination,
        cat_ce=radii,
        confidence_terms=terms,
        rooms=rooms,
        templates=templates,
        bullseye_name=bullseye_name,
        bullseye_latitude=bullseye_lat,
        bullseye_longitude=bullseye_lon,
        schema_version=schema_version,
    )


def _type_from_mapping(entry: dict[str, object]) -> CoordType:
    name = str(entry.get("name", entry.get("type", "")) or "").strip()
    letters = _letters(entry.get("kinds", entry.get("kind")))
    if not name:
        raise CoordCardError("A coord card type needs a name.")
    if not letters:
        raise CoordCardError(f"{name} kind must be one or more letters.")
    start = _whole(name, "start", entry.get("start"))
    end = _whole(name, "end", entry.get("end"))
    if start > end:
        raise CoordCardError(f"{name} range start is after end.")
    fields = _fields(name, entry.get("fields"))
    type_rooms = _type_rooms(entry.get("rooms")) if "rooms" in entry else None
    return CoordType(
        name=name,
        kind=letters[0],
        start=start,
        end=end,
        fields=fields,
        kinds=letters,
        rooms=type_rooms,
    )


def _fields(name: str, raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        tokens = [part.strip().lower() for part in raw.replace(";", ",").split(",")]
    elif isinstance(raw, list):
        tokens = [_field_name(name, item) for item in raw]
    else:
        raise CoordCardError(f"{name} needs an ordered fields list.")
    names = tuple(token for token in tokens if token)
    if "id" not in names:
        raise CoordCardError(f"{name} fields must include id.")
    if len(names) != len(set(names)):
        raise CoordCardError(f"{name} repeats a field.")
    return names


def _field_name(type_name: str, item: object) -> str:
    if isinstance(item, str):
        return item.strip().lower()
    if isinstance(item, dict):
        raw = item.get("name", item.get("role", ""))
        return str(raw or "").strip().lower()
    raise CoordCardError(f"{type_name} field must be a name.")


def _reject_overlaps(types: list[CoordType]) -> None:
    by_kind: dict[str, list[tuple[int, int]]] = {}
    for item in types:
        for letter in item.kinds:
            by_kind.setdefault(letter, []).append((item.start, item.end))
    for kind, blocks in by_kind.items():
        ordered = sorted(blocks)
        previous_end = ordered[0][1]
        for start, end in ordered[1:]:
            if start <= previous_end:
                raise CoordCardError(f"kind {kind} blocks overlap.")
            previous_end = end


def _schema_version(value: object) -> str:
    from analytics.domain.card_schema import SchemaCatalogError, version_text

    if value is None or value == "":
        return ""
    try:
        return version_text(value)
    except SchemaCatalogError as exc:
        raise CoordCardError(str(exc)) from exc


def _csv_document(text: str) -> dict[str, object]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CoordCardError("Coord card CSV needs a header.")
    header = {name.strip().lower() for name in reader.fieldnames if name}
    required = {"name", "kind", "start", "end", "fields"}
    if not required <= header:
        raise CoordCardError("Coord card CSV needs name, kind, start, end, and fields.")
    types: list[dict[str, object]] = []
    templates: list[dict[str, object]] = []
    distance = 0.0
    distance_set = False
    schema_version = ""
    ingest: list[str] = []
    ingest_set = False
    default_rooms: list[str] = []
    rules: list[dict[str, object]] = []
    publish_set = False
    republish = True
    for row in reader:
        lowered: dict[str, str] = {}
        for key, value in row.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            lowered[key.strip().lower()] = value.strip()
        if not any(lowered.values()):
            continue
        if lowered.get("mismatch_meters"):
            distance = _number("mismatch_meters", lowered["mismatch_meters"])
            distance_set = True
        if lowered.get("schema_version"):
            schema_version = lowered["schema_version"]
        if lowered.get("ingest_rooms"):
            ingest = _room_names(lowered["ingest_rooms"])
            ingest_set = True
        if lowered.get("publish_default"):
            default_rooms = _room_names(lowered["publish_default"])
            publish_set = True
        if lowered.get("republish_on_higher_level"):
            republish = lowered["republish_on_higher_level"].strip().lower() in {"1", "true", "yes"}
            publish_set = True
        rule = _csv_rule(lowered)
        if rule is not None:
            rules.append(rule)
            publish_set = True
        if lowered.get("template"):
            templates.append(_csv_template(lowered))
            continue
        if not lowered.get("name"):
            continue
        kind = lowered.get("kinds") or lowered.get("kind", "")
        entry: dict[str, object] = {
            "name": lowered.get("name", ""),
            "kind": kind,
            "start": lowered.get("start", ""),
            "end": lowered.get("end", ""),
            "fields": lowered.get("fields", ""),
        }
        type_rooms = _csv_type_rooms(lowered)
        if type_rooms is not None:
            entry["rooms"] = type_rooms
        types.append(entry)
    payload: dict[str, object] = {"types": types}
    if distance_set:
        payload["mismatch_meters"] = distance
    if schema_version:
        payload["schema_version"] = schema_version
    if ingest_set or publish_set:
        rooms: dict[str, object] = {}
        if ingest_set:
            rooms["ingest"] = ingest
        if publish_set:
            rooms["publish"] = {
                "default": default_rooms,
                "republish_on_higher_level": republish,
                "rules": rules,
            }
        payload["rooms"] = rooms
    if templates:
        payload["templates"] = templates
    return payload


def _looks_like_csv(text: str) -> bool:
    first = text.splitlines()[0].strip().lower()
    return first.startswith("name,") or first.startswith("type,")


def _label(letter: str, block: CoordType) -> str:
    return f"{letter}-{block.start}..{letter}-{block.end}"


def _letters(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        tokens = re_split_kinds(raw)
    elif isinstance(raw, list):
        tokens = [str(item).strip().upper() for item in raw]
    else:
        tokens = []
    letters = tuple(token for token in tokens if token)
    for letter in letters:
        if len(letter) != 1 or not letter.isalpha():
            raise CoordCardError(f"kind {letter!r} must be one letter.")
    if len(letters) != len(set(letters)):
        raise CoordCardError("A coord card type repeats a kind letter.")
    return letters


def re_split_kinds(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.replace("/", " ").replace(",", " ").split()]


def _ellipse_unit(value: object) -> str:
    text = str(value or "nmi").strip().lower()
    if text in {"nmi", "nm", "nautical", "nautical_miles"}:
        return "nmi"
    if text in {"km", "kilometer", "kilometers"}:
        return "km"
    if text in {"m", "meter", "meters"}:
        return "meters"
    raise CoordCardError("ellipse_unit must be nmi, km, or meters.")


def type_side(card: CoordCard, type_name: str, side: str) -> tuple[str, ...] | None:
    """Rooms the card sets for one type or template. None means unset."""

    from analytics.domain.rooms import type_key
    from analytics.domain.templates import MessageTemplate

    target = type_key(type_name)
    for item in card.types:
        if type_key(item.name) != target or item.rooms is None:
            continue
        chosen = item.rooms.ingest if side == "ingest" else item.rooms.publish
        if chosen is not None:
            return chosen
    for template in card.templates:
        if not isinstance(template, MessageTemplate):
            continue
        if type_key(template.name) != target or template.rooms is None:
            continue
        chosen = template.rooms.ingest if side == "ingest" else template.rooms.publish
        if chosen is not None:
            return chosen
    return None


def card_default_rooms(card: CoordCard, side: str) -> tuple[str, ...] | None:
    """Card-level ingest list or publish default. None means the card omitted it."""

    if card.rooms is None:
        return None
    if side == "ingest":
        return card.rooms.ingest
    if card.rooms.publish is None:
        return None
    return card.rooms.publish.default_rooms


def _templates(raw: object) -> tuple[object, ...]:
    from analytics.domain.templates import TemplateError, templates_from_mapping

    try:
        return templates_from_mapping(raw)
    except TemplateError as exc:
        raise CoordCardError(str(exc)) from exc


def _type_rooms(raw: object) -> TypeRooms | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CoordCardError("type rooms must be a mapping.")
    return TypeRooms(
        ingest=_optional_rooms(raw, "ingest"),
        publish=_optional_rooms(raw, "publish"),
    )


def _optional_rooms(raw: dict[str, object], key: str) -> tuple[str, ...] | None:
    if key not in raw:
        return None
    return tuple(_room_names(raw.get(key)))


def _csv_type_rooms(row: dict[str, str]) -> dict[str, object] | None:
    found: dict[str, object] = {}
    if row.get("ingest"):
        found["ingest"] = _room_names(row["ingest"])
    if row.get("publish"):
        found["publish"] = _room_names(row["publish"])
    if not found:
        return None
    return found


def _csv_template(row: dict[str, str]) -> dict[str, object]:
    name = row.get("name") or row.get("template", "")
    separator = row.get("template", "lines")
    if separator.lower() in {"yes", "true", "1"}:
        separator = row.get("separator", "lines") or "lines"
    fields = _csv_template_fields(row.get("fields", ""))
    item: dict[str, object] = {
        "name": name,
        "detect": row.get("detect", ""),
        "separator": separator,
        "fields": fields,
        "evidence": row.get("evidence", "report") or "report",
        "classification": row.get("classification", "").strip().lower() in {"1", "true", "yes"},
    }
    rooms = _csv_type_rooms(row)
    if rooms is not None:
        item["rooms"] = rooms
    return item


def _csv_template_fields(text: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for part in text.split(";"):
        piece = part.strip()
        if not piece:
            continue
        bits = [item.strip() for item in piece.split(":")]
        entry: dict[str, object] = {"name": bits[0], "type": bits[1] if len(bits) > 1 else "text"}
        if len(bits) > 2 and bits[2]:
            entry["line"] = bits[2]
        if len(bits) > 3 and bits[3]:
            entry["pattern"] = bits[3]
        found.append(entry)
    return found


def _rooms(raw: object) -> CardRooms:
    if not isinstance(raw, dict):
        raise CoordCardError("rooms must be a mapping.")
    ingest = None
    if "ingest" in raw:
        ingest = tuple(_room_names(raw.get("ingest")))
    publish = None
    if "publish" in raw:
        try:
            publish = table_from_mapping(raw.get("publish"))
        except PublishTableError as exc:
            raise CoordCardError(str(exc)) from exc
    return CardRooms(ingest=ingest, publish=publish)


def _room_names(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        tokens = value.replace("|", ",").replace(";", ",").split(",")
    elif isinstance(value, list):
        tokens = [str(item) for item in value]
    else:
        raise CoordCardError("Room names must be a list.")
    return [token.strip() for token in tokens if token.strip() and token.strip() != "-"]


def _csv_rule(row: dict[str, str]) -> dict[str, object] | None:
    rooms = row.get("publish_rooms", "")
    confidence = row.get("confidence", "")
    if not rooms and not confidence and not row.get("cat_min") and not row.get("rule_types"):
        return None
    rule: dict[str, object] = {"rooms": _room_names(rooms.replace("|", ","))}
    if confidence:
        rule["confidence"] = confidence.replace("|", ",")
    if row.get("cat_min"):
        rule["cat_min"] = row["cat_min"]
    if row.get("cat_max"):
        rule["cat_max"] = row["cat_max"]
    if row.get("rule_types"):
        rule["types"] = row["rule_types"].replace("|", ",")
    if row.get("rule_sources"):
        rule["sources"] = row["rule_sources"].replace("|", ",")
    return rule


def _bullseye(raw: object) -> tuple[str, float | None, float | None]:
    if raw is None or raw == "":
        return "", None, None
    if isinstance(raw, str):
        return raw.strip(), None, None
    if not isinstance(raw, dict):
        raise CoordCardError("bullseye must be a name or a mapping.")
    name = str(raw.get("name", "") or "").strip()
    latitude = raw.get("latitude")
    longitude = raw.get("longitude")
    lat = None if latitude in (None, "") else _number("bullseye latitude", latitude)
    lon = None if longitude in (None, "") else _number("bullseye longitude", longitude)
    return name, lat, lon


def _whole(name: str, field: str, value: object) -> int:
    number = _number(f"{name} {field}", value)
    if number != int(number):
        raise CoordCardError(f"{name} {field} must be an integer.")
    whole = int(number)
    if whole < 0 or whole > 999:
        raise CoordCardError(f"{name} {field} must be a three-digit serial.")
    return whole


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CoordCardError(f"{name} must be a number.")
    try:
        return float(value)
    except ValueError:
        raise CoordCardError(f"{name} must be a number.") from None
