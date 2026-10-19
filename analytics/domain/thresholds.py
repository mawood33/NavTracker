from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisThresholds:
    """Configurable heuristics. Distances are meters, speeds are m/s.

    Slowing down: the previous speed is above ``slow_speed_mps`` and the
    next speed is at most ``slowdown_ratio`` times that previous speed.

    Stopping: speed stays at or below ``stop_speed_mps``. A stop is
    reported once its dwell reaches ``min_dwell_seconds``.

    Milling: at least ``mill_min_vehicles`` vehicles have stops within
    ``mill_radius_meters`` whose time ranges overlap by
    ``mill_min_overlap_seconds``.

    Areas of interest cluster stops within ``aoi_radius_meters``.

    Hide site: one or few vehicles (``hide_max_vehicles``), a dwell of at
    least ``hide_min_dwell_seconds``, and the cluster is outside every
    MSR corridor.

    Launch site: at least ``launch_min_visits`` stops whose median dwell
    sits between ``launch_min_dwell_seconds`` and
    ``launch_max_dwell_seconds``.

    Transload site: at least ``transload_min_vehicles`` vehicles, total
    dwell of ``transload_min_dwell_seconds``, and the cluster is inside
    an MSR corridor.

    A known site's types are kept. Each heuristic that matches adds its
    type, so one area can be hide and launch together. Confidence is a
    separate policy in ``analytics.domain.assessment``.
    """

    dedupe_tolerance_seconds: float = 0
    slow_speed_mps: float = 5.0
    slowdown_ratio: float = 0.5
    stop_speed_mps: float = 1.0
    min_dwell_seconds: float = 120
    mill_radius_meters: float = 150
    mill_min_vehicles: int = 2
    mill_min_overlap_seconds: float = 180
    aoi_radius_meters: float = 250
    hide_min_dwell_seconds: float = 1800
    hide_max_vehicles: int = 2
    transload_min_vehicles: int = 2
    transload_min_dwell_seconds: float = 300
    launch_min_visits: int = 3
    launch_min_dwell_seconds: float = 60
    launch_max_dwell_seconds: float = 600
    site_match_meters: float = 100
    msr_corridor_meters: float = 50
    revisit_gap_seconds: float = 3600
