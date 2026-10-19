"""Grid, confidence, correlation, report policy, and track thresholds."""

from dataclasses import fields

from analytics.domain.assessment import ConfidencePolicy
from analytics.domain.correlation import CorrelationPolicy
from analytics.domain.grid import GridPolicy
from analytics.domain.reports import ReportPolicy
from analytics.domain.thresholds import AnalysisThresholds
from analytics.infrastructure.config_values import (
    _float_fields,
    _nested,
    _non_negative,
    _positive,
)
from navtracker_common.settings import pick_setting

_INT_THRESHOLDS = {
    "mill_min_vehicles",
    "hide_max_vehicles",
    "transload_min_vehicles",
    "launch_min_visits",
}

def _thresholds(analytics: dict[str, object]) -> AnalysisThresholds:
    defaults = AnalysisThresholds()
    floats: dict[str, float] = {}
    ints: dict[str, int] = {}
    for field in fields(defaults):
        env_name = "ANALYTICS_" + field.name.upper()
        raw = pick_setting(
            env_name,
            analytics.get(field.name),
            getattr(defaults, field.name),
        )
        number = _non_negative(env_name, raw)
        if field.name in _INT_THRESHOLDS:
            if number != int(number):
                raise RuntimeError(
                    f"Invalid {env_name}={raw!r}. Expected an integer."
                )
            ints[field.name] = int(number)
        else:
            floats[field.name] = number
    return AnalysisThresholds(
        dedupe_tolerance_seconds=floats["dedupe_tolerance_seconds"],
        slow_speed_mps=floats["slow_speed_mps"],
        slowdown_ratio=floats["slowdown_ratio"],
        stop_speed_mps=floats["stop_speed_mps"],
        min_dwell_seconds=floats["min_dwell_seconds"],
        mill_radius_meters=floats["mill_radius_meters"],
        mill_min_vehicles=ints["mill_min_vehicles"],
        mill_min_overlap_seconds=floats["mill_min_overlap_seconds"],
        aoi_radius_meters=floats["aoi_radius_meters"],
        hide_min_dwell_seconds=floats["hide_min_dwell_seconds"],
        hide_max_vehicles=ints["hide_max_vehicles"],
        transload_min_vehicles=ints["transload_min_vehicles"],
        transload_min_dwell_seconds=floats["transload_min_dwell_seconds"],
        launch_min_visits=ints["launch_min_visits"],
        launch_min_dwell_seconds=floats["launch_min_dwell_seconds"],
        launch_max_dwell_seconds=floats["launch_max_dwell_seconds"],
        site_match_meters=floats["site_match_meters"],
        msr_corridor_meters=floats["msr_corridor_meters"],
        revisit_gap_seconds=floats["revisit_gap_seconds"],
    )

def _confidence(analytics: dict[str, object]) -> ConfidencePolicy:
    raw = _nested(analytics.get("confidence"), "analytics.confidence")
    defaults = ConfidencePolicy()
    values = _float_fields("ANALYTICS_CONFIDENCE_", raw, defaults)
    policy = ConfidencePolicy(
        initial_confidence=values["initial_confidence"],
        probable_at=values["probable_at"],
        confirmed_at=values["confirmed_at"],
        window_seconds=values["window_seconds"],
        type_evidence_at=values["type_evidence_at"],
        stop_weight=values["stop_weight"],
        dwell_weight_per_minute=values["dwell_weight_per_minute"],
        dwell_cap=values["dwell_cap"],
        mill_weight=values["mill_weight"],
        extra_vehicle_weight=values["extra_vehicle_weight"],
        revisit_weight=values["revisit_weight"],
        msr_transload_weight=values["msr_transload_weight"],
        msr_adjacency_weight=values["msr_adjacency_weight"],
        off_msr_hide_weight=values["off_msr_hide_weight"],
        cell_score_scale=values["cell_score_scale"],
        cell_cap=values["cell_cap"],
        decay_per_day=values["decay_per_day"],
        decay_after_seconds=values["decay_after_seconds"],
        max_confidence=values["max_confidence"],
    )
    if policy.probable_at > policy.confirmed_at:
        raise RuntimeError(
            "ANALYTICS_CONFIDENCE_PROBABLE_AT must be <= "
            "ANALYTICS_CONFIDENCE_CONFIRMED_AT."
        )
    if policy.initial_confidence > policy.max_confidence:
        raise RuntimeError(
            "ANALYTICS_CONFIDENCE_INITIAL_CONFIDENCE must be <= "
            "ANALYTICS_CONFIDENCE_MAX_CONFIDENCE."
        )
    return policy

def _grid(analytics: dict[str, object]) -> GridPolicy:
    raw = _nested(analytics.get("grid"), "analytics.grid")
    defaults = GridPolicy()
    values = _float_fields("ANALYTICS_GRID_", raw, defaults)
    resolution = values["resolution"]
    if resolution != int(resolution) or resolution > 15:
        raise RuntimeError(
            "Invalid ANALYTICS_GRID_RESOLUTION="
            f"{resolution!r}. Expected an integer from 0 to 15."
        )
    return GridPolicy(
        resolution=int(resolution),
        window_seconds=values["window_seconds"],
        decay_half_life_seconds=values["decay_half_life_seconds"],
        hotspot_score=values["hotspot_score"],
        stop_weight=values["stop_weight"],
        dwell_weight_per_minute=values["dwell_weight_per_minute"],
        dwell_cap=values["dwell_cap"],
        extra_vehicle_weight=values["extra_vehicle_weight"],
        mill_weight=values["mill_weight"],
        msr_weight=values["msr_weight"],
    )

def _report_policy(analytics: dict[str, object]) -> ReportPolicy:
    raw = _nested(analytics.get("reports"), "analytics.reports")
    defaults = ReportPolicy()
    window_seconds = _non_negative(
        "ANALYTICS_REPORTS_WINDOW_SECONDS",
        pick_setting(
            "ANALYTICS_REPORTS_WINDOW_SECONDS",
            raw.get("window_seconds"),
            defaults.window_seconds,
        ),
    )
    weight = _non_negative(
        "ANALYTICS_REPORTS_WEIGHT",
        pick_setting("ANALYTICS_REPORTS_WEIGHT", raw.get("weight"), defaults.weight),
    )
    type_weight = _non_negative(
        "ANALYTICS_REPORTS_TYPE_WEIGHT",
        pick_setting(
            "ANALYTICS_REPORTS_TYPE_WEIGHT",
            raw.get("type_weight"),
            defaults.type_weight,
        ),
    )
    cap = _non_negative(
        "ANALYTICS_REPORTS_CAP",
        pick_setting("ANALYTICS_REPORTS_CAP", raw.get("cap"), defaults.cap),
    )
    return ReportPolicy(
        window_seconds=window_seconds,
        weight=weight,
        type_weight=type_weight,
        cap=cap,
        hide_keywords=_keywords(raw, "hide", defaults.hide_keywords),
        launch_keywords=_keywords(raw, "launch", defaults.launch_keywords),
        transload_keywords=_keywords(raw, "transload", defaults.transload_keywords),
    )

def _keywords(
    raw: dict[str, object],
    site_type: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    block = raw.get("keywords")
    if not isinstance(block, dict) or site_type not in block:
        return default
    value = block[site_type]
    if isinstance(value, list):
        return tuple(str(item).strip().lower() for item in value if str(item).strip())
    return tuple(part.strip().lower() for part in str(value).split(",") if part.strip())

def _correlation(analytics: dict[str, object]) -> CorrelationPolicy:
    raw = _nested(analytics.get("correlation"), "analytics.correlation")
    defaults = CorrelationPolicy()
    sources = _positive(
        "ANALYTICS_CORRELATION_MINIMUM_SOURCES",
        pick_setting(
            "ANALYTICS_CORRELATION_MINIMUM_SOURCES",
            raw.get("minimum_sources"),
            defaults.minimum_sources,
        ),
    )
    if sources != int(sources):
        raise RuntimeError(
            "Invalid ANALYTICS_CORRELATION_MINIMUM_SOURCES. Expected an integer."
        )
    return CorrelationPolicy(
        window_minutes=_positive(
            "ANALYTICS_CORRELATION_WINDOW_MINUTES",
            pick_setting(
                "ANALYTICS_CORRELATION_WINDOW_MINUTES",
                raw.get("window_minutes"),
                defaults.window_minutes,
            ),
        ),
        hide_gap_minutes=_non_negative(
            "ANALYTICS_CORRELATION_HIDE_GAP_MINUTES",
            pick_setting(
                "ANALYTICS_CORRELATION_HIDE_GAP_MINUTES",
                raw.get("hide_gap_minutes"),
                defaults.hide_gap_minutes,
            ),
        ),
        minimum_correlation_score=_non_negative(
            "ANALYTICS_CORRELATION_MINIMUM_SCORE",
            pick_setting(
                "ANALYTICS_CORRELATION_MINIMUM_SCORE",
                raw.get("minimum_score"),
                defaults.minimum_correlation_score,
            ),
        ),
        minimum_sources=int(sources),
        minimum_transload_speed_mps=_non_negative(
            "ANALYTICS_CORRELATION_MINIMUM_TRANSLOAD_SPEED_MPS",
            pick_setting(
                "ANALYTICS_CORRELATION_MINIMUM_TRANSLOAD_SPEED_MPS",
                raw.get("minimum_transload_speed_mps"),
                defaults.minimum_transload_speed_mps,
            ),
        ),
        explicit_link_weight=_non_negative(
            "ANALYTICS_CORRELATION_EXPLICIT_LINK_WEIGHT",
            pick_setting(
                "ANALYTICS_CORRELATION_EXPLICIT_LINK_WEIGHT",
                raw.get("explicit_link_weight"),
                defaults.explicit_link_weight,
            ),
        ),
    )

def grid_settings(analytics: dict[str, object]) -> dict[str, object]:
    return {
        "thresholds": _thresholds(analytics),
        "confidence": _confidence(analytics),
        "grid": _grid(analytics),
        "report_policy": _report_policy(analytics),
        "correlation": _correlation(analytics),
    }
