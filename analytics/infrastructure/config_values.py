"""Shared parsers for analytics settings sections."""

from dataclasses import fields

from analytics.domain.assessment import ConfidencePolicy
from analytics.domain.grid import GridPolicy
from navtracker_common.settings import bool_setting, pick_setting


def _mapping(value: object, name: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a mapping.")
    return value

def _nested(value: object, name: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a mapping.")
    return value

def _choice(name: str, value: object, allowed: set[str]) -> str:
    text = str(value).strip().lower()
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise RuntimeError(f"Invalid {name}={value!r}. Expected one of: {choices}.")
    return text

def _bool_setting(name: str, value: object) -> bool:
    return bool_setting(name, value)

def _positive(name: str, value: object) -> float:
    number = _non_negative(name, value)
    if number <= 0:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number greater than 0.")
    return number

def _non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.")
    try:
        number = float(value)
    except ValueError:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.") from None
    if number < 0:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number >= 0.")
    return number

def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.")
    try:
        return float(value)
    except ValueError:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.") from None

def _byte_setting(name: str, configured: object, default: int) -> int:
    number = _positive(name, pick_setting(name, configured, default))
    if number != int(number):
        raise RuntimeError(f"Invalid {name}={configured!r}. Expected an integer.")
    return int(number)

def _string_list(env_name: str, yaml_value: object) -> tuple[str, ...]:
    raw = pick_setting(env_name, yaml_value, "")
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())

def _float_fields(
    prefix: str,
    raw: dict[str, object],
    defaults: ConfidencePolicy | GridPolicy,
) -> dict[str, float]:
    values: dict[str, float] = {}
    for field in fields(defaults):
        env_name = prefix + field.name.upper()
        values[field.name] = _non_negative(
            env_name,
            pick_setting(env_name, raw.get(field.name), getattr(defaults, field.name)),
        )
    return values

