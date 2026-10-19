"""Per-type ChatSurfer rooms and confidence/CAT publish rules."""

import os

from analytics.domain.rooms import parse_room_setting
from analytics.infrastructure.config_values import (
    _bool_setting,
    _non_negative,
)
from navtracker_common.settings import env_setting, pick_setting


def _default_rooms(
    env_name: str,
    analytics: dict[str, object],
    key: str,
) -> tuple[str, ...]:
    raw = pick_setting(env_name, _yaml_room_block(analytics).get(key), "")
    parsed = parse_room_setting(_room_text(raw))
    if parsed is None:
        return ()
    return parsed

def _typed_rooms(
    suffix: str,
    analytics: dict[str, object],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    found: dict[str, tuple[str, ...]] = {}
    block = _yaml_room_block(analytics).get(suffix.lower())
    if isinstance(block, dict):
        for key, value in block.items():
            parsed = parse_room_setting(_room_text(value))
            if parsed is not None:
                found[str(key)] = parsed
    prefix = "ANALYTICS_ROOMS_"
    tail = f"_{suffix}"
    for key, value in os.environ.items():
        if not key.startswith(prefix) or not key.endswith(tail):
            continue
        middle = key[len(prefix) : -len(tail)]
        if middle in {"DEFAULT", ""}:
            continue
        parsed = parse_room_setting(value)
        if parsed is not None:
            found[middle] = parsed
    return tuple(sorted(found.items()))

def _yaml_room_block(analytics: dict[str, object]) -> dict[str, object]:
    raw = analytics.get("message_rooms")
    if isinstance(raw, dict):
        return raw
    return {}

def _room_text(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value)

def room_settings(analytics: dict[str, object]) -> dict[str, object]:
    return {
        "publish_events": _bool_setting(
                    "ANALYTICS_PUBLISH_EVENTS",
                    pick_setting(
                        "ANALYTICS_PUBLISH_EVENTS",
                        analytics.get("publish_events"),
                        False,
                    ),
                ),
        "room_ingest": _typed_rooms("INGEST", analytics),
        "room_publish": _typed_rooms("PUBLISH", analytics),
        "default_ingest_rooms": _default_rooms(
            "ANALYTICS_ROOMS_DEFAULT_INGEST",
            analytics,
            "ingest",
        ),
        "default_publish_rooms": _default_rooms(
                    "ANALYTICS_ROOMS_DEFAULT_PUBLISH",
                    analytics,
                    "publish",
                ),
        "publish_rules_text": env_setting(
                    "ANALYTICS_PUBLISH_RULES",
                    str(analytics.get("publish_rules", "") or ""),
                ),
        "republish_on_higher_level": _bool_setting(
                    "ANALYTICS_PUBLISH_REPUBLISH_ON_HIGHER_LEVEL",
                    pick_setting(
                        "ANALYTICS_PUBLISH_REPUBLISH_ON_HIGHER_LEVEL",
                        analytics.get("republish_on_higher_level"),
                        True,
                    ),
                ),
        "vacant_evidence_weight": _non_negative(
                    "ANALYTICS_VACANT_EVIDENCE_WEIGHT",
                    pick_setting(
                        "ANALYTICS_VACANT_EVIDENCE_WEIGHT",
                        analytics.get("vacant_evidence_weight"),
                        0.25,
                    ),
                ),
    }
