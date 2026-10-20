"""Persisted analyst override for published coordinates."""

import json
import logging
from pathlib import Path

from analytics.application.settings import AnalyticsSettings
from analytics.domain.coordinates import (
    CoordinateFormatError,
    OutputCoordinates,
    coordinate_document,
    output_from_mapping,
    resolve_output_coordinates,
)

logger = logging.getLogger(__name__)
_FILE_NAME = "output-coordinates.json"


def override_file(settings: AnalyticsSettings) -> Path:
    directory = Path(settings.reference_dir.strip() or "reference")
    return directory / _FILE_NAME


def env_coordinates(settings: AnalyticsSettings) -> OutputCoordinates:
    return OutputCoordinates(
        format=settings.output_coord_format,
        precision_m=settings.output_mgrs_precision_m,
        spaces=settings.output_mgrs_spaces,
        decimals=settings.output_latlon_decimals,
        separator=settings.output_latlon_separator,
        hemisphere=settings.output_latlon_hemisphere,
    )


def load_override(path: Path) -> OutputCoordinates | None:
    """Return the saved override, or None when the analyst is using the card."""

    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring output coordinate override %s: %s", path, exc)
        return None
    if loaded in (None, {}, {"use_coord_card": True}):
        return None
    try:
        return output_from_mapping(loaded)
    except CoordinateFormatError as exc:
        logger.warning("Ignoring output coordinate override %s: %s", path, exc)
        return None


def save_override(path: Path, spec: OutputCoordinates | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec is None:
        path.write_text('{"use_coord_card": true}\n', encoding="utf-8")
        return
    payload = coordinate_document(spec, source="override")
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def describe_resolution(
    settings: AnalyticsSettings,
    card: OutputCoordinates | None,
    override: OutputCoordinates | None,
    message_type: str = "",
) -> tuple[OutputCoordinates, str]:
    return resolve_output_coordinates(
        override,
        card,
        env_coordinates(settings),
        env_configured=settings.output_coord_configured,
        message_type=message_type,
    )
