"""Site confidence and type evidence.

Player Data imports start as ``possible`` with ``initial_confidence``.
Activity inside ``window_seconds`` adds explainable evidence and can
raise confidence. Status follows the confidence thresholds
``probable_at`` and ``confirmed_at``. Decay is off unless
``decay_per_day`` is greater than zero.

``assess_site`` is the only function that changes confidence. ``Site``
and ``Evidence`` in ``analytics.domain.reference`` stay the shared
model. ``analytics.application.service.AnalyticsService.analyze`` is
the call site.

Activity and correlation rows from ``analytics.domain.correlation``
are evidence. An accepted transition is weight 1.0 and a rejected one
is 0.0. Numeric confidence scales those weights by
``accepted_evidence_scale`` so a single acceptance stays below
``confirmed_at``. An ``explicit_link`` row is not scaled: its weight
is the configured explicit-correlation boost. A ``probable`` correlation label raises status to
``probable``. A ``truth`` row, including manual verification, forces
``confirmed``. The TransloadOracle word ``verified`` is the external
name for ``confirmed``. ``confirmed`` is also reachable from the
thresholds. Site types stay a set. Decay stays off unless configured.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from analytics.domain.aoi import AreaOfInterest
from analytics.domain.behavior import Mill, Stop
from analytics.domain.correlation import Correlation, correlation_evidence
from analytics.domain.geo import haversine_meters, point_in_polygon
from analytics.domain.grid import CellActivity, CellIndex
from analytics.domain.reference import Evidence, Msr, Site, match_msr
from analytics.domain.reports import Report, ReportPolicy, select_reports
from analytics.domain.thresholds import AnalysisThresholds

_DURABLE_KINDS = frozenset({"import", "declared_type", "discovered", "truth"})
_SCALED_KINDS = frozenset({"activity", "correlation"})


@dataclass(frozen=True)
class ConfidencePolicy:
    """Defaults keep an untouched import at ``possible``.

    ``decay_per_day`` of 0 disables decay. A positive value subtracts
    that much confidence for each day after ``decay_after_seconds``
    without nearby activity.
    """

    initial_confidence: float = 0.2
    probable_at: float = 0.55
    confirmed_at: float = 0.8
    window_seconds: float = 86_400
    type_evidence_at: float = 0.15
    stop_weight: float = 0.05
    dwell_weight_per_minute: float = 0.02
    dwell_cap: float = 0.25
    mill_weight: float = 0.15
    extra_vehicle_weight: float = 0.05
    revisit_weight: float = 0.08
    msr_transload_weight: float = 0.06
    msr_adjacency_weight: float = 0.04
    off_msr_hide_weight: float = 0.15
    cell_score_scale: float = 0.05
    cell_cap: float = 0.2
    accepted_evidence_scale: float = 0.2
    decay_per_day: float = 0.0
    decay_after_seconds: float = 86_400
    max_confidence: float = 1.0


@dataclass(frozen=True)
class SiteActivity:
    """Activity associated with one site. The window filter is already applied."""

    as_of: datetime
    stops: int
    dwell_seconds: float
    distinct_vehicles: int
    revisits: int
    milling: bool
    on_msr: bool
    long_dwell: bool
    cell_id: str
    cell_score: float
    cell_hotspot: bool
    reports: tuple[Report, ...]
    last_activity_at: datetime | None
    pending_evidence: tuple[Evidence, ...] = ()
    correlation_probable: bool = False


def status_for(confidence: float, policy: ConfidencePolicy) -> str:
    if confidence >= policy.confirmed_at:
        return "confirmed"
    if confidence >= policy.probable_at:
        return "probable"
    return "possible"


def stamp_import(site: Site, policy: ConfidencePolicy) -> Site:
    """Mark a Player Data point as possible at the initial confidence."""

    declared = tuple(
        Evidence(
            kind="declared_type",
            weight=0.0,
            detail=f"file declared {site_type}",
            site_type=site_type,
        )
        for site_type in sorted(site.site_types)
    )
    evidence = (
        Evidence(
            kind="import",
            weight=policy.initial_confidence,
            detail="Player Data import starts as possible",
        ),
        *declared,
    )
    return replace(
        site,
        status="possible",
        confidence=policy.initial_confidence,
        evidence=evidence,
        origin="import",
    )


def site_from_area(
    area: AreaOfInterest,
    policy: ConfidencePolicy,
    observed_at: datetime,
) -> Site:
    """Turn an unmatched area into a possible site.

    Inferred types are recorded as discovered evidence and kept on the
    site. Confidence starts at ``initial_confidence``, not at confirmed.
    """

    hints = tuple(
        Evidence(
            kind="discovered",
            weight=0.0,
            detail=f"activity suggested {site_type}",
            site_type=site_type,
            observed_at=observed_at,
        )
        for site_type in sorted(area.supported_types)
    )
    evidence = (
        Evidence(
            kind="discovered",
            weight=policy.initial_confidence,
            detail="AOI had no matching known site",
            observed_at=observed_at,
        ),
        *hints,
    )
    return Site(
        name=f"discovered-{area.latitude:.5f}-{area.longitude:.5f}",
        site_types=area.supported_types,
        status="possible",
        confidence=policy.initial_confidence,
        evidence=evidence,
        origin="discovered",
        latitude=area.latitude,
        longitude=area.longitude,
    )


def assess_site(
    site: Site,
    activity: SiteActivity,
    policy: ConfidencePolicy,
    report_policy: ReportPolicy | None = None,
) -> Site:
    """Recompute confidence from durable evidence plus this window.

    Activity evidence replaces the previous window instead of stacking,
    so a repeated call with the same activity is stable. Declared types
    stay. A type is added once its tagged evidence reaches
    ``type_evidence_at``.
    """

    fresh = _activity_evidence(activity, policy, report_policy or ReportPolicy())
    evidence = _durable(site.evidence) + fresh + activity.pending_evidence
    confidence = _clamp(_numeric_confidence(evidence, policy), policy)
    types = set(site.site_types)
    for site_type in ("hide", "launch", "transload"):
        tagged = sum(item.weight for item in evidence if item.site_type == site_type)
        if tagged >= policy.type_evidence_at:
            types.add(site_type)
    status = status_for(confidence, policy)
    verified = any(item.kind == "truth" for item in evidence)
    if verified:
        status = "confirmed"
        confidence = _clamp(max(confidence, policy.confirmed_at), policy)
    elif (
        activity.correlation_probable
        and status != "confirmed"
        and confidence < policy.confirmed_at
    ):
        status = "probable"
        if confidence < policy.probable_at:
            confidence = policy.probable_at
    return replace(
        site,
        site_types=frozenset(types),
        confidence=confidence,
        status=status,
        evidence=evidence,
        activity_status=_activity_status(activity, site),
        verified=verified,
    )


def activity_for_site(
    site: Site,
    stops: Sequence[Stop],
    mills: Sequence[Mill],
    msrs: Sequence[Msr],
    as_of: datetime,
    policy: ConfidencePolicy,
    thresholds: AnalysisThresholds,
    cell: CellActivity | None = None,
    reports: Sequence[Report] = (),
    report_policy: ReportPolicy | None = None,
) -> SiteActivity:
    """Collect stops and mills near the site inside the confidence window."""

    window_start = as_of - timedelta(seconds=policy.window_seconds)
    nearby = [stop for stop in stops if _near_site(site, stop, thresholds.site_match_meters)]
    in_window = [
        stop
        for stop in nearby
        if stop.end >= window_start and stop.start <= as_of
    ]
    last = max((stop.end for stop in nearby if stop.end <= as_of), default=None)
    vehicles = tuple(dict.fromkeys(stop.vehicle_id for stop in in_window))
    dwell_seconds = sum(stop.dwell_seconds for stop in in_window)
    milling = any(
        _mill_near(site, mill, thresholds.site_match_meters)
        and mill.end >= window_start
        and mill.start <= as_of
        for mill in mills
    )
    latitude = site.latitude
    longitude = site.longitude
    on_msr = False
    if latitude is not None and longitude is not None:
        on_msr = (
            match_msr(latitude, longitude, msrs, thresholds.msr_corridor_meters)
            is not None
        )
    return SiteActivity(
        as_of=as_of,
        stops=len(in_window),
        dwell_seconds=dwell_seconds,
        distinct_vehicles=len(vehicles),
        revisits=max(0, _visits(in_window, thresholds.revisit_gap_seconds) - 1),
        milling=milling,
        on_msr=on_msr,
        long_dwell=dwell_seconds >= thresholds.hide_min_dwell_seconds,
        cell_id="" if cell is None else cell.cell_id,
        cell_score=0.0 if cell is None else cell.score,
        cell_hotspot=False if cell is None else cell.hotspot,
        reports=select_reports(
            reports,
            latitude=latitude,
            longitude=longitude,
            polygon=site.polygon,
            as_of=as_of,
            policy=report_policy or ReportPolicy(),
            max_meters=thresholds.site_match_meters,
        ),
        last_activity_at=last,
    )


def update_sites(
    sites: Sequence[Site],
    areas: Sequence[AreaOfInterest],
    stops: Sequence[Stop],
    mills: Sequence[Mill],
    msrs: Sequence[Msr],
    as_of: datetime,
    policy: ConfidencePolicy,
    thresholds: AnalysisThresholds,
    cells: Sequence[CellActivity] = (),
    index: CellIndex | None = None,
    reports: Sequence[Report] = (),
    report_policy: ReportPolicy | None = None,
    correlations: Sequence[Correlation] = (),
) -> tuple[Site, ...]:
    """Assess sites, add unmatched areas, then add unmatched hotspot cells."""

    by_id = {cell.cell_id: cell for cell in cells}
    assessed: list[Site] = []
    names: set[str] = set()
    for site in sites:
        activity = _with_correlations(
            activity_for_site(
                site,
                stops,
                mills,
                msrs,
                as_of,
                policy,
                thresholds,
                _cell_at(site, by_id, index),
                reports,
                report_policy,
            ),
            site,
            correlations,
            thresholds,
        )
        updated = assess_site(site, activity, policy, report_policy)
        assessed.append(updated)
        names.add(updated.name)
    for area in areas:
        if area.matched_site is not None or not area.supported_types:
            continue
        discovered = site_from_area(area, policy, as_of)
        if discovered.name in names:
            continue
        activity = activity_for_site(
            discovered,
            stops,
            mills,
            msrs,
            as_of,
            policy,
            thresholds,
            _cell_at(discovered, by_id, index),
            reports,
            report_policy,
        )
        assessed.append(assess_site(discovered, activity, policy, report_policy))
        names.add(discovered.name)
    for cell in cells:
        if not cell.hotspot or _near_existing(cell, assessed, thresholds):
            continue
        discovered = site_from_cell(cell, policy, as_of, thresholds)
        if discovered.name in names:
            continue
        activity = activity_for_site(
            discovered,
            stops,
            mills,
            msrs,
            as_of,
            policy,
            thresholds,
            cell,
            reports,
            report_policy,
        )
        assessed.append(assess_site(discovered, activity, policy, report_policy))
        names.add(discovered.name)
    for correlation in correlations:
        if _near_correlation(correlation, assessed, thresholds):
            continue
        observed = correlation.activity.updated_at
        discovered = Site(
            name=f"activity-{correlation.activity.activity_id.replace(':', '-')}",
            status="possible",
            confidence=policy.initial_confidence,
            evidence=(
                Evidence(
                    kind="discovered",
                    weight=policy.initial_confidence,
                    detail="activity had no matching site",
                    observed_at=observed,
                ),
            ),
            origin="discovered",
            latitude=correlation.activity.latitude,
            longitude=correlation.activity.longitude,
            moa=correlation.activity.bmoa,
        )
        if discovered.name in names:
            continue
        activity = _with_correlations(
            activity_for_site(
                discovered,
                stops,
                mills,
                msrs,
                as_of,
                policy,
                thresholds,
                None,
                reports,
                report_policy,
            ),
            discovered,
            (correlation,),
            thresholds,
        )
        assessed.append(assess_site(discovered, activity, policy, report_policy))
        names.add(discovered.name)
    return tuple(assessed)


def site_from_cell(
    cell: CellActivity,
    policy: ConfidencePolicy,
    observed_at: datetime,
    thresholds: AnalysisThresholds,
) -> Site:
    """Turn an unmatched hotspot hex into a possible site."""

    types: set[str] = set()
    if cell.on_msr and len(cell.vehicle_ids) >= thresholds.transload_min_vehicles:
        types.add("transload")
    if (
        cell.long_dwell
        and not cell.on_msr
        and len(cell.vehicle_ids) <= thresholds.hide_max_vehicles
    ):
        types.add("hide")
    if cell.revisits + 1 >= thresholds.launch_min_visits:
        types.add("launch")
    hints = tuple(
        Evidence(
            kind="discovered",
            weight=0.0,
            detail=f"hex {cell.cell_id} suggested {site_type}",
            site_type=site_type,
            observed_at=observed_at,
        )
        for site_type in sorted(types)
    )
    evidence = (
        Evidence(
            kind="discovered",
            weight=policy.initial_confidence,
            detail=f"hotspot hex {cell.cell_id} had no matching site",
            observed_at=observed_at,
        ),
        *hints,
    )
    return Site(
        name=f"cell-{cell.cell_id}",
        site_types=frozenset(types),
        status="possible",
        confidence=policy.initial_confidence,
        evidence=evidence,
        origin="discovered",
        latitude=cell.latitude,
        longitude=cell.longitude,
    )


def _cell_at(
    site: Site,
    cells: dict[str, CellActivity],
    index: CellIndex | None,
) -> CellActivity | None:
    if index is None or site.latitude is None or site.longitude is None:
        return None
    return cells.get(index.cell_id(site.latitude, site.longitude))


def _near_existing(
    cell: CellActivity,
    sites: Sequence[Site],
    thresholds: AnalysisThresholds,
) -> bool:
    for site in sites:
        if site.polygon and point_in_polygon(
            cell.latitude, cell.longitude, site.polygon
        ):
            return True
        if site.latitude is None or site.longitude is None:
            continue
        distance = haversine_meters(
            cell.latitude, cell.longitude, site.latitude, site.longitude
        )
        if distance <= thresholds.site_match_meters:
            return True
    return False


def merge_catalog(
    imported: Sequence[Site],
    working: Sequence[Site],
) -> tuple[Site, ...]:
    """Keep assessed imports and discovered sites when files are reloaded.

    A site already assessed under the same name keeps its confidence and
    evidence. Geometry and file-declared types come from the new import.
    Discovered sites that the files do not name are kept.
    """

    working_by_name = {site.name: site for site in working}
    merged: list[Site] = []
    names: set[str] = set()
    for site in imported:
        prior = working_by_name.get(site.name)
        if prior is not None and prior.origin == "import":
            merged.append(_refresh_import(prior, site))
        else:
            merged.append(site)
        names.add(site.name)
    for site in working:
        if site.origin == "discovered" and site.name not in names:
            merged.append(site)
            names.add(site.name)
    return tuple(merged)


def _refresh_import(prior: Site, fresh: Site) -> Site:
    declared = {
        item.site_type
        for item in prior.evidence
        if item.kind == "declared_type" and item.site_type
    }
    extra = tuple(
        Evidence(
            kind="declared_type",
            weight=0.0,
            detail=f"file declared {site_type}",
            site_type=site_type,
        )
        for site_type in sorted(fresh.site_types - declared)
    )
    return replace(
        prior,
        latitude=fresh.latitude,
        longitude=fresh.longitude,
        polygon=fresh.polygon,
        moa=fresh.moa,
        complex_name=fresh.complex_name,
        mgrs=fresh.mgrs,
        site_types=prior.site_types | fresh.site_types,
        evidence=prior.evidence + extra,
        origin="import",
    )


def _activity_evidence(
    activity: SiteActivity,
    policy: ConfidencePolicy,
    report_policy: ReportPolicy,
) -> tuple[Evidence, ...]:
    found: list[Evidence] = []
    when = activity.as_of
    if activity.stops:
        found.append(
            Evidence(
                kind="stop",
                weight=policy.stop_weight * activity.stops,
                detail=f"{activity.stops} stops inside the window",
                observed_at=when,
            )
        )
    dwell_weight = min(
        policy.dwell_cap,
        (activity.dwell_seconds / 60.0) * policy.dwell_weight_per_minute,
    )
    if dwell_weight > 0:
        found.append(
            Evidence(
                kind="dwell",
                weight=dwell_weight,
                detail=f"dwell {activity.dwell_seconds:.0f}s inside the window",
                observed_at=when,
            )
        )
    if activity.milling:
        found.append(
            Evidence(
                kind="mill",
                weight=policy.mill_weight,
                detail="vehicles milled together",
                site_type="transload",
                observed_at=when,
            )
        )
    if activity.distinct_vehicles >= 2:
        extra = activity.distinct_vehicles - 1
        found.append(
            Evidence(
                kind="vehicles",
                weight=policy.extra_vehicle_weight * extra,
                detail=f"{activity.distinct_vehicles} vehicles",
                site_type="transload",
                observed_at=when,
            )
        )
    if activity.revisits:
        found.append(
            Evidence(
                kind="revisit",
                weight=policy.revisit_weight * activity.revisits,
                detail=f"{activity.revisits} revisits",
                site_type="launch",
                observed_at=when,
            )
        )
    if activity.on_msr and activity.stops:
        found.append(
            Evidence(
                kind="msr",
                weight=policy.msr_transload_weight,
                detail="activity on an MSR",
                site_type="transload",
                observed_at=when,
            )
        )
    if activity.long_dwell and not activity.on_msr:
        found.append(
            Evidence(
                kind="off_msr",
                weight=policy.off_msr_hide_weight,
                detail="long dwell off the MSR network",
                site_type="hide",
                observed_at=when,
            )
        )
    if activity.cell_score > 0:
        tagged = "transload" if activity.cell_hotspot and activity.on_msr else ""
        found.append(
            Evidence(
                kind="cell",
                weight=min(
                    policy.cell_cap,
                    activity.cell_score * policy.cell_score_scale,
                ),
                detail=(
                    f"hex {activity.cell_id} score {activity.cell_score:.2f}"
                ),
                site_type=tagged,
                observed_at=when,
            )
        )
    found.extend(_report_evidence(activity, report_policy))
    if not found and policy.decay_per_day > 0 and activity.last_activity_at is not None:
        idle = (activity.as_of - activity.last_activity_at).total_seconds()
        if idle > policy.decay_after_seconds:
            days = (idle - policy.decay_after_seconds) / 86_400
            drop = policy.decay_per_day * days
            if drop > 0:
                found.append(
                    Evidence(
                        kind="decay",
                        weight=-drop,
                        detail=f"no activity for {idle:.0f}s",
                        observed_at=when,
                    )
                )
    if activity.on_msr and policy.msr_adjacency_weight > 0:
        found.append(
            Evidence(
                kind="msr_adjacency",
                weight=policy.msr_adjacency_weight,
                detail="site lies in an MSR corridor",
                site_type="transload",
                observed_at=when,
            )
        )
    return tuple(found)


def _report_evidence(
    activity: SiteActivity,
    policy: ReportPolicy,
) -> list[Evidence]:
    found: list[Evidence] = []
    used = 0.0
    for report in activity.reports:
        if used >= policy.cap:
            break
        weight = min(policy.weight, policy.cap - used)
        if weight <= 0:
            break
        used += weight
        found.append(
            Evidence(
                kind="report",
                weight=weight,
                detail=report.summary or "report",
                observed_at=report.observed_at,
                source=report.source,
                layer=report.layer,
                message_id=report.message_id,
            )
        )
        if report.site_type and used < policy.cap:
            extra = min(policy.type_weight, policy.cap - used)
            if extra <= 0:
                continue
            used += extra
            found.append(
                Evidence(
                    kind="report",
                    weight=extra,
                    detail=f"{report.site_type} from {report.layer}",
                    site_type=report.site_type,
                    observed_at=report.observed_at,
                    source=report.source,
                    layer=report.layer,
                    message_id=report.message_id,
                )
            )
    return found


def _with_correlations(
    activity: SiteActivity,
    site: Site,
    correlations: Sequence[Correlation],
    thresholds: AnalysisThresholds,
) -> SiteActivity:
    related = tuple(
        item for item in correlations if _correlation_matches(site, item, thresholds)
    )
    if not related:
        return activity
    pending = tuple(
        evidence for item in related for evidence in correlation_evidence(item)
    )
    return replace(
        activity,
        pending_evidence=activity.pending_evidence + pending,
        correlation_probable=any(item.label == "probable" for item in related),
    )


def _correlation_matches(
    site: Site,
    correlation: Correlation,
    thresholds: AnalysisThresholds,
) -> bool:
    latitude = correlation.activity.latitude
    longitude = correlation.activity.longitude
    if site.polygon and point_in_polygon(latitude, longitude, site.polygon):
        return True
    if site.latitude is None or site.longitude is None:
        return False
    return (
        haversine_meters(latitude, longitude, site.latitude, site.longitude)
        <= thresholds.site_match_meters
    )


def _near_correlation(
    correlation: Correlation,
    sites: Sequence[Site],
    thresholds: AnalysisThresholds,
) -> bool:
    return any(_correlation_matches(site, correlation, thresholds) for site in sites)


def _numeric_confidence(evidence: Sequence[Evidence], policy: ConfidencePolicy) -> float:
    total = 0.0
    for item in evidence:
        if item.kind in _SCALED_KINDS:
            total += item.weight * policy.accepted_evidence_scale
        else:
            total += item.weight
    return total


def _activity_status(activity: SiteActivity, site: Site) -> str:
    if (
        activity.stops
        or activity.dwell_seconds > 0
        or activity.milling
        or activity.reports
        or activity.pending_evidence
    ):
        return "active"
    if activity.last_activity_at is not None:
        return "inactive"
    return site.activity_status or "unknown"


def _durable(evidence: Sequence[Evidence]) -> tuple[Evidence, ...]:
    return tuple(item for item in evidence if item.kind in _DURABLE_KINDS)


def _clamp(value: float, policy: ConfidencePolicy) -> float:
    if value < 0:
        return 0.0
    if value > policy.max_confidence:
        return policy.max_confidence
    return value


def _near_site(site: Site, stop: Stop, max_meters: float) -> bool:
    if site.polygon and point_in_polygon(stop.latitude, stop.longitude, site.polygon):
        return True
    if site.latitude is None or site.longitude is None:
        return False
    return (
        haversine_meters(
            stop.latitude, stop.longitude, site.latitude, site.longitude
        )
        <= max_meters
    )


def _mill_near(site: Site, mill: Mill, max_meters: float) -> bool:
    if site.latitude is None or site.longitude is None:
        return False
    return (
        haversine_meters(
            mill.latitude, mill.longitude, site.latitude, site.longitude
        )
        <= max_meters
    )


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
