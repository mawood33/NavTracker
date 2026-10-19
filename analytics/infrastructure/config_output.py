"""Published coordinate format from the environment and the analytics YAML."""

import os

from analytics.domain.coordinates import CoordinateFormatError, output_from_mapping
from navtracker_common.settings import pick_setting

_KEYS = (
    ("ANALYTICS_OUTPUT_LATLON_HEMISPHERE", "latlon_hemisphere"),
    ("ANALYTICS_OUTPUT_LATLON_SEPARATOR", "latlon_separator"),
    ("ANALYTICS_OUTPUT_LATLON_DECIMALS", "latlon_decimals"),
    ("ANALYTICS_OUTPUT_MGRS_PRECISION_M", "mgrs_precision_m"),
    ("ANALYTICS_OUTPUT_COORD_FORMAT", "coordinate_format"),
    ("ANALYTICS_OUTPUT_MGRS_SPACES", "mgrs_spaces"),
)


def output_coord_settings(analytics: dict[str, object]) -> dict[str, object]:
    raw_block = analytics.get("output")
    block: dict[str, object] = {}
    if raw_block is not None:
        if not isinstance(raw_block, dict):
            raise RuntimeError("Invalid ANALYTICS_OUTPUT_COORD_FORMAT: output must be a mapping.")
        block = raw_block
    configured = _configured(block)
    payload = {
        "coordinate_format": pick_setting(
            "ANALYTICS_OUTPUT_COORD_FORMAT",
            block.get("coordinate_format"),
            "mgrs",
        ),
        "mgrs_precision_m": pick_setting(
            "ANALYTICS_OUTPUT_MGRS_PRECISION_M",
            block.get("mgrs_precision_m"),
            1,
        ),
        "mgrs_spaces": pick_setting(
            "ANALYTICS_OUTPUT_MGRS_SPACES",
            block.get("mgrs_spaces"),
            False,
        ),
        "latlon_decimals": pick_setting(
            "ANALYTICS_OUTPUT_LATLON_DECIMALS",
            block.get("latlon_decimals"),
            5,
        ),
        "latlon_separator": pick_setting(
            "ANALYTICS_OUTPUT_LATLON_SEPARATOR",
            block.get("latlon_separator"),
            "space",
        ),
        "latlon_hemisphere": pick_setting(
            "ANALYTICS_OUTPUT_LATLON_HEMISPHERE",
            block.get("latlon_hemisphere"),
            "suffix",
        ),
    }
    try:
        spec = output_from_mapping(payload)
    except CoordinateFormatError as exc:
        raise RuntimeError(f"Invalid ANALYTICS_OUTPUT_COORD_FORMAT: {exc}") from exc
    return {
        "output_coord_format": spec.format,
        "output_mgrs_precision_m": spec.precision_m,
        "output_mgrs_spaces": spec.spaces,
        "output_latlon_decimals": spec.decimals,
        "output_latlon_separator": spec.separator,
        "output_latlon_hemisphere": spec.hemisphere,
        "output_coord_configured": configured,
    }


def _configured(block: dict[str, object]) -> bool:
    for env_name, yaml_name in _KEYS:
        if os.environ.get(env_name, "").strip():
            return True
        if block.get(yaml_name) not in (None, ""):
            return True
    return False
