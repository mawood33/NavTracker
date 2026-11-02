"""Read TACREP text and Navixy JSON for development replay.

File bytes stay here so the HTTP handler and the scripts share one
parser. Navixy ``gps.speed`` is kilometers per hour and is converted to
meters per second, the same conversion as live ingest. A replay object
that already has ``speed_mps`` is left in meters per second.
"""

import csv
import io
import json
from collections.abc import Mapping
from pathlib import Path

from navtracker_common.units import kmh_to_mps


def read_tacrep_file(path: Path) -> str:
    """Return one TACREP body. ``.json`` and ``.csv`` become ``//`` lines."""

    data = path.read_bytes()
    texts = tacrep_texts(path.name, data)
    return "\n".join(texts)


def tacrep_texts(name: str, data: bytes) -> tuple[str, ...]:
    """Split a txt, json, or csv payload into TACREP bodies."""

    suffix = Path(name).suffix.lower()
    text = data.decode("utf-8-sig")
    if suffix == ".json" or _looks_like_json(text):
        return _tacrep_json(text)
    if suffix == ".csv":
        return _tacrep_csv(text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return tuple(lines)


def read_navixy_file(path: Path) -> tuple[dict[str, object], ...]:
    """Return fix mappings with ``speed_mps`` already converted."""

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return tuple(fixes_from_navixy(payload))


def fixes_from_navixy(payload: object) -> list[dict[str, object]]:
    """Accept a fix list, ``{"fixes": [...]}``, ``get_state``, or ``state_batch``."""

    found: list[dict[str, object]] = []
    for item in _navixy_items(payload):
        fix = _one_fix(item)
        if fix is not None:
            found.append(fix)
    return found


def _tacrep_json(text: str) -> tuple[str, ...]:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError("TACREP JSON is not valid") from exc
    if isinstance(payload, str):
        body = payload.strip()
        return (body,) if body else ()
    if isinstance(payload, list):
        return tuple(_text_item(item) for item in payload if _text_item(item))
    if isinstance(payload, dict):
        for key in ("tacrep", "message", "body", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return (value.strip(),)
        raw = payload.get("messages", payload.get("events", payload.get("records")))
        if isinstance(raw, list):
            return tuple(_text_item(item) for item in raw if _text_item(item))
    raise ValueError("TACREP JSON must be a string, a list, or an object with messages")


def _tacrep_csv(text: str) -> tuple[str, ...]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ()
    header = [cell.strip().lower() for cell in rows[0]]
    start = 0
    column = 0
    for name in ("tacrep", "message", "body", "text"):
        if name in header:
            column = header.index(name)
            start = 1
            break
    found: list[str] = []
    for row in rows[start:]:
        if column >= len(row):
            continue
        body = row[column].strip()
        if body and not body.startswith("#"):
            found.append(body)
    return tuple(found)


def _text_item(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("tacrep", "message", "body", "text"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _navixy_items(payload: object) -> list[Mapping[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("Navixy replay must be a JSON object or list")
    fixes = payload.get("fixes")
    if isinstance(fixes, list):
        return [item for item in fixes if isinstance(item, dict)]
    state = payload.get("state")
    if isinstance(state, dict):
        return [state]
    data = payload.get("data")
    kind = str(payload.get("type", payload.get("event", "")) or "")
    if kind in {"state_batch", "state"} and isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if kind == "state" and isinstance(data, dict):
        return [data]
    return [payload]


def _one_fix(item: Mapping[str, object]) -> dict[str, object] | None:
    gps = item.get("gps")
    gps_map = gps if isinstance(gps, dict) else {}
    location = gps_map.get("location")
    location_map = location if isinstance(location, dict) else {}
    latitude = _float(item.get("latitude", item.get("lat", location_map.get("lat"))))
    longitude = _float(
        item.get("longitude", item.get("lon", item.get("lng", location_map.get("lng"))))
    )
    vehicle = _vehicle(item)
    timestamp = item.get("timestamp", gps_map.get("updated", item.get("updated")))
    if (
        vehicle is None
        or latitude is None
        or longitude is None
        or not isinstance(timestamp, str)
        or not timestamp.strip()
    ):
        return None
    heading = _float(item.get("heading", gps_map.get("heading")))
    return {
        "vehicle_id": vehicle,
        "latitude": latitude,
        "longitude": longitude,
        "timestamp": timestamp.strip(),
        "speed_mps": _speed_mps(item, gps_map),
        "heading": heading,
    }


def _vehicle(item: Mapping[str, object]) -> str | None:
    for key in ("vehicle_id", "tracker_id", "id", "source_id"):
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _speed_mps(item: Mapping[str, object], gps: Mapping[str, object]) -> float | None:
    direct = _float(item.get("speed_mps"))
    if direct is not None:
        return direct
    if gps:
        speed = _float(gps.get("speed", item.get("speed")))
        if speed is None:
            return None
        return kmh_to_mps(speed)
    speed = _float(item.get("speed"))
    if speed is None:
        return None
    unit = str(item.get("speed_unit", "mps") or "mps").strip().lower()
    if unit in {"kmh", "km/h"}:
        return kmh_to_mps(speed)
    return speed


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
