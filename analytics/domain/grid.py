"""H3 hex aggregation of stops.

TransloadOracle was not readable from this environment, so the grid
system is not copied from that repository. H3 is the recommended
replacement: one global hex index, a configurable resolution, and a
stable cell id a frontend can join to a hex tile. Resolution 9 is the
default (about a 174 m edge), matching the extracted TransloadOracle
setting ``h3_resolution``. Resolution 8 is the wider alternative
(about 460 m).

Each cell keeps the stops inside ``window_seconds``. When
``decay_half_life_seconds`` is 0, which is the default, every stop in
the window counts in full and older stops drop out. A positive
half-life halves a stop's contribution once it is that old, and the
window still discards anything older than ``window_seconds``.

A cell on an MSR corridor gets ``msr_weight`` added to its score.
``hotspot`` is a score at or above ``hotspot_score``.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from analytics.domain.behavior import Mill, Stop
from analytics.domain.reference import Msr, match_msr
from analytics.domain.reports import Report, ReportPolicy, reports_by_cell
from analytics.domain.thresholds import AnalysisThresholds


class CellIndex(Protocol):
    """Maps positions to hex cells. ``H3Index`` is the production adapter."""

    def cell_id(self, latitude: float, longitude: float) -> str:
        """Return the cell that contains the point."""

    def center(self, cell_id: str) -> tuple[float, float]:
        """Return the cell center as latitude, longitude."""

    def boundary(self, cell_id: str) -> tuple[tuple[float, float], ...]:
        """Return the ring as latitude, longitude pairs."""


@dataclass(frozen=True)
class GridPolicy:
    """Hex scoring. Distances elsewhere stay on ``AnalysisThresholds``."""

    resolution: int = 9
    window_seconds: float = 86_400
    decay_half_life_seconds: float = 0.0
    hotspot_score: float = 1.5
    stop_weight: float = 1.0
    dwell_weight_per_minute: float = 0.2
    dwell_cap: float = 2.0
    extra_vehicle_weight: float = 0.5
    mill_weight: float = 0.75
    msr_weight: float = 0.5


@dataclass(frozen=True)
class CellActivity:
    """One hex cell. ``boundary`` points are (latitude, longitude)."""

    cell_id: str
    latitude: float
    longitude: float
    boundary: tuple[tuple[float, float], ...]
    score: float
    stops: int
    dwell_seconds: float
    vehicle_ids: tuple[str, ...]
    revisits: int
    milling: bool
    on_msr: bool
    long_dwell: bool
    hotspot: bool
    reports: tuple[Report, ...] = ()


def aggregate_cells(
    stops: Sequence[Stop],
    mills: Sequence[Mill],
    msrs: Sequence[Msr],
    as_of: datetime,
    policy: GridPolicy,
    thresholds: AnalysisThresholds,
    index: CellIndex,
    reports: Sequence[Report] = (),
    report_policy: ReportPolicy | None = None,
) -> tuple[CellActivity, ...]:
    """Score each hex that has a stop or a report inside the window."""

    window_start = as_of - timedelta(seconds=policy.window_seconds)
    rules = report_policy or ReportPolicy()
    grouped_reports = reports_by_cell(reports, as_of, rules, index.cell_id)
    grouped: dict[str, list[Stop]] = {}
    for stop in stops:
        if stop.end < window_start or stop.start > as_of:
            continue
        cell_id = index.cell_id(stop.latitude, stop.longitude)
        grouped.setdefault(cell_id, []).append(stop)
    mill_cells = {
        index.cell_id(mill.latitude, mill.longitude)
        for mill in mills
        if mill.end >= window_start and mill.start <= as_of
    }
    cells: list[CellActivity] = []
    for cell_id in sorted(set(grouped) | set(grouped_reports)):
        cells.append(
            _score_cell(
                cell_id,
                grouped.get(cell_id, []),
                cell_id in mill_cells,
                msrs,
                as_of,
                policy,
                thresholds,
                index,
                grouped_reports.get(cell_id, ()),
            )
        )
    return tuple(cells)


def _score_cell(
    cell_id: str,
    stops: Sequence[Stop],
    milling: bool,
    msrs: Sequence[Msr],
    as_of: datetime,
    policy: GridPolicy,
    thresholds: AnalysisThresholds,
    index: CellIndex,
    cell_reports: Sequence[Report] = (),
) -> CellActivity:
    score = 0.0
    dwell = 0.0
    for stop in stops:
        factor = _decay_factor(as_of, stop.end, policy.decay_half_life_seconds)
        score += policy.stop_weight * factor
        dwell_weight = min(
            policy.dwell_cap,
            (stop.dwell_seconds / 60.0) * policy.dwell_weight_per_minute,
        )
        score += dwell_weight * factor
        dwell += stop.dwell_seconds
    vehicles = tuple(dict.fromkeys(stop.vehicle_id for stop in stops))
    if len(vehicles) >= 2:
        score += policy.extra_vehicle_weight * (len(vehicles) - 1)
    if milling:
        score += policy.mill_weight
    latitude, longitude = index.center(cell_id)
    on_msr = (
        match_msr(latitude, longitude, msrs, thresholds.msr_corridor_meters)
        is not None
    )
    if on_msr and stops:
        score += policy.msr_weight
    revisits = max(0, _visits(stops, thresholds.revisit_gap_seconds) - 1)
    return CellActivity(
        cell_id=cell_id,
        latitude=latitude,
        longitude=longitude,
        boundary=index.boundary(cell_id),
        score=score,
        stops=len(stops),
        dwell_seconds=dwell,
        vehicle_ids=vehicles,
        revisits=revisits,
        milling=milling,
        on_msr=on_msr,
        long_dwell=dwell >= thresholds.hide_min_dwell_seconds,
        hotspot=score >= policy.hotspot_score,
        reports=tuple(cell_reports),
    )


def _decay_factor(as_of: datetime, when: datetime, half_life_seconds: float) -> float:
    if half_life_seconds <= 0:
        return 1.0
    age = max(0.0, (as_of - when).total_seconds())
    factor: float = math.pow(0.5, age / half_life_seconds)
    return factor


def _visits(stops: Sequence[Stop], revisit_gap_seconds: float) -> int:
    per_vehicle: dict[str, list[Stop]] = {}
    for stop in sorted(stops, key=lambda item: (item.vehicle_id, item.start)):
        per_vehicle.setdefault(stop.vehicle_id, []).append(stop)
    count = 0
    for group in per_vehicle.values():
        count += 1
        previous = group[0].end
        for stop in group[1:]:
            gap = (stop.start - previous).total_seconds()
            if gap >= revisit_gap_seconds:
                count += 1
            if stop.end > previous:
                previous = stop.end
    return count
