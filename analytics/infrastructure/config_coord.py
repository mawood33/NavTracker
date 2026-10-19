"""Coord card path, ellipse, CAT/CE radii, and confidence terms."""



from analytics.domain.catce import DEFAULT_CAT_CE_METERS, CatTableError, parse_cat_ce
from analytics.domain.confidence_text import (
    DEFAULT_CONFIDENCE_TERMS,
    ConfidenceTableError,
    terms_from_mapping,
    terms_from_text,
)
from analytics.infrastructure.config_values import (
    _finite,
    _non_negative,
)
from navtracker_common.settings import env_setting, pick_setting


def _cat_ce(analytics: dict[str, object]) -> tuple[tuple[int, float], ...]:
    raw = pick_setting("ANALYTICS_CAT_CE", analytics.get("cat_ce"), "")
    if raw in ("", None):
        return DEFAULT_CAT_CE_METERS
    try:
        return parse_cat_ce(raw)
    except CatTableError as exc:
        raise RuntimeError(f"Invalid ANALYTICS_CAT_CE: {exc}") from exc

def _confidence_terms(analytics: dict[str, object]) -> tuple[object, ...]:
    raw = pick_setting("ANALYTICS_CONFIDENCE_TERMS", analytics.get("confidence_terms"), "")
    if raw in ("", None):
        return DEFAULT_CONFIDENCE_TERMS
    try:
        if isinstance(raw, list):
            return terms_from_mapping(raw)
        return terms_from_text(str(raw))
    except (ConfidenceTableError, ValueError) as exc:
        raise RuntimeError(f"Invalid ANALYTICS_CONFIDENCE_TERMS: {exc}") from exc

def _ellipse_unit(analytics: dict[str, object]) -> str:
    raw = pick_setting(
        "ANALYTICS_ELLIPSE_UNIT",
        analytics.get("ellipse_unit"),
        "nmi",
    )
    text = str(raw).strip().lower()
    if text in {"nmi", "nm", "nautical", "nautical_miles"}:
        return "nmi"
    if text in {"km", "kilometer", "kilometers"}:
        return "km"
    if text in {"m", "meter", "meters"}:
        return "meters"
    raise RuntimeError("Invalid ANALYTICS_ELLIPSE_UNIT. Expected nmi, km, or meters.")

def coord_settings(analytics: dict[str, object]) -> dict[str, object]:
    return {
        "coord_card_path": env_setting(
                    "ANALYTICS_COORD_CARD",
                    str(analytics.get("coord_card", "")),
                ).strip(),
        "coord_mismatch_meters": _non_negative(
                    "ANALYTICS_COORD_MISMATCH_METERS",
                    pick_setting(
                        "ANALYTICS_COORD_MISMATCH_METERS",
                        analytics.get("coord_mismatch_meters"),
                        250,
                    ),
                ),
        "ellipse_unit": _ellipse_unit(analytics),
        "magnetic_declination_degrees": _finite(
                    "ANALYTICS_MAGNETIC_DECLINATION",
                    pick_setting(
                        "ANALYTICS_MAGNETIC_DECLINATION",
                        analytics.get("magnetic_declination_degrees"),
                        0,
                    ),
                ),
        "cat_ce": _cat_ce(analytics),
        "confidence_terms": _confidence_terms(analytics),
        "coord_card_schema_path": env_setting(
            "ANALYTICS_COORD_CARD_SCHEMA",
            str(analytics.get("coord_card_schema", "")),
        ).strip(),
    }
