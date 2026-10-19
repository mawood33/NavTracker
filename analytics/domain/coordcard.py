"""Exercise coord card: TACREP types, id blocks, and field order.

Block numbers change per exercise. Nothing in this module invents a
range. A card lists each type name, kind letter, inclusive serial
range, and the ordered fields for that type. ``id`` is required.
Missing values on a message are optional. Speeds on the card are miles
per hour. Latitude and longitude are one decimal-degree field.
"""

import csv
import io
from dataclasses import dataclass
from typing import Protocol

import yaml


class CoordCardError(ValueError):
    """The text is not a card that can replace the one already loaded."""


class MgrsGrid(Protocol):
    """Converts one MGRS string to latitude and longitude."""

    def to_latlon(self, text: str) -> tuple[float, float] | None:
        """Return the point, or None when the string is not MGRS."""


@dataclass(frozen=True)
class CoordType:
    """One TACREP type on the exercise card."""

    name: str
    kind: str
    start: int
    end: int
    fields: tuple[str, ...]


@dataclass(frozen=True)
class CoordCard:
    """The loaded card. An empty card classifies no blocks."""

    types: tuple[CoordType, ...] = ()
    mismatch_meters: float = 250.0

    def match(self, kind: str, serial: int) -> tuple[CoordType | None, str, str]:
        """Return the type, block label, and warning. The id is kept."""

        letter = kind.upper()
        configured = [item for item in self.types if item.kind == letter]
        matching = [item for item in configured if item.start <= serial <= item.end]
        if matching:
            chosen = matching[0]
            return chosen, _label(chosen), ""
        if not configured:
            return None, "", f"unknown TACREP type for kind {letter}"
        label = ", ".join(_label(item) for item in configured)
        warning = f"serial {letter}-{serial:03d} outside configured block {label}"
        if len(configured) == 1:
            return configured[0], label, warning
        return None, label, warning


def parse_coord_card_text(
    text: str,
    *,
    suffix: str = "",
    mismatch_meters: float = 250.0,
) -> CoordCard:
    """Parse YAML or CSV. Raises ``CoordCardError`` and does not apply."""

    body = text.strip()
    if not body:
        raise CoordCardError("Coord card is empty.")
    if suffix.lower().endswith(".csv") or _looks_like_csv(body):
        return _from_csv(body, mismatch_meters)
    try:
        loaded = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise CoordCardError("Coord card is not valid YAML or CSV.") from exc
    if not isinstance(loaded, dict):
        raise CoordCardError("Coord card must be a mapping.")
    return card_from_mapping(loaded, mismatch_meters)


def card_from_mapping(payload: dict[str, object], mismatch_meters: float) -> CoordCard:
    """Validate a decoded card. Overlapping blocks for one letter fail."""

    raw_types = payload.get("types", payload.get("tacreps"))
    if not isinstance(raw_types, list) or not raw_types:
        raise CoordCardError("Coord card needs a types list.")
    mismatch = payload.get("mismatch_meters", mismatch_meters)
    distance = _number("mismatch_meters", mismatch)
    if distance < 0:
        raise CoordCardError("mismatch_meters must be >= 0.")
    types: list[CoordType] = []
    for entry in raw_types:
        if not isinstance(entry, dict):
            raise CoordCardError("Each coord card type must be a mapping.")
        types.append(_type_from_mapping(entry))
    _reject_overlaps(types)
    return CoordCard(types=tuple(types), mismatch_meters=distance)


def _type_from_mapping(entry: dict[str, object]) -> CoordType:
    name = str(entry.get("name", entry.get("type", "")) or "").strip()
    kind = str(entry.get("kind", "") or "").strip().upper()
    if not name:
        raise CoordCardError("A coord card type needs a name.")
    if len(kind) != 1 or not kind.isalpha():
        raise CoordCardError(f"{name} kind must be one letter.")
    start = _whole(name, "start", entry.get("start"))
    end = _whole(name, "end", entry.get("end"))
    if start > end:
        raise CoordCardError(f"{name} range start is after end.")
    fields = _fields(name, entry.get("fields"))
    return CoordType(name=name, kind=kind, start=start, end=end, fields=fields)


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
    by_kind: dict[str, list[CoordType]] = {}
    for item in types:
        by_kind.setdefault(item.kind, []).append(item)
    for kind, blocks in by_kind.items():
        ordered = sorted(blocks, key=lambda block: (block.start, block.end))
        previous = ordered[0]
        for block in ordered[1:]:
            if block.start <= previous.end:
                raise CoordCardError(f"kind {kind} blocks overlap.")
            previous = block


def _from_csv(text: str, mismatch_meters: float) -> CoordCard:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CoordCardError("Coord card CSV needs a header.")
    header = {name.strip().lower() for name in reader.fieldnames if name}
    required = {"name", "kind", "start", "end", "fields"}
    if not required <= header:
        raise CoordCardError("Coord card CSV needs name, kind, start, end, and fields.")
    types: list[dict[str, object]] = []
    distance = mismatch_meters
    for row in reader:
        lowered = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        if not any(lowered.values()):
            continue
        if lowered.get("mismatch_meters"):
            distance = _number("mismatch_meters", lowered["mismatch_meters"])
        types.append(
            {
                "name": lowered.get("name", ""),
                "kind": lowered.get("kind", ""),
                "start": lowered.get("start", ""),
                "end": lowered.get("end", ""),
                "fields": lowered.get("fields", ""),
            }
        )
    return card_from_mapping({"types": types, "mismatch_meters": distance}, mismatch_meters)


def _looks_like_csv(text: str) -> bool:
    first = text.splitlines()[0].strip().lower()
    return first.startswith("name,") or first.startswith("type,")


def _label(block: CoordType) -> str:
    return f"{block.kind}-{block.start}..{block.kind}-{block.end}"


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
