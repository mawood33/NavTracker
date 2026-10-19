"""Correlate tracks and reports. Loops live in the orchestrator."""

from datetime import UTC, datetime

from analytics.application.state import AnalyticsState
from analytics.domain.aoi import derive_areas
from analytics.domain.assessment import (
    update_sites,
)
from analytics.domain.behavior import (
    detect_milling,
    detect_slowdowns,
    detect_stops,
)
from analytics.domain.correlation import (
    Activity,
    manual_advance,
    state_site_type,
)
from analytics.domain.grid import aggregate_cells
from analytics.domain.reports import (
    select_route_reports,
)
from analytics.domain.timeutil import ensure_utc
from analytics.domain.tracks import dedupe_fixes


class AnalysisService:
    def __init__(self, state: AnalyticsState) -> None:
        self._state = state

    def analyze(self) -> None:
        state = self._state
        deduped = dedupe_fixes(
            state._fixes,
            state.settings.thresholds.dedupe_tolerance_seconds,
        )
        state._slowdowns = detect_slowdowns(deduped, state.settings.thresholds)
        state._stops = detect_stops(deduped, state.settings.thresholds)
        state._mills = detect_milling(state._stops, state.settings.thresholds)
        state._areas = derive_areas(
            state._stops,
            state._sites,
            state._msrs,
            state.settings.thresholds,
        )
        as_of = max(
            (ensure_utc(fix.timestamp) for fix in deduped),
            default=datetime.now(UTC),
        )
        as_of = max(
            [as_of, *(ensure_utc(report.observed_at) for report in state._reports)],
        )
        state._cells = aggregate_cells(
            state._stops,
            state._mills,
            state._msrs,
            as_of,
            state.settings.grid,
            state.settings.thresholds,
            state._index,
            state._reports,
            state.settings.report_policy,
        )
        state._refresh_correlations()
        state._sites = update_sites(
            state._sites,
            state._areas,
            state._stops,
            state._mills,
            state._msrs,
            as_of,
            state.settings.confidence,
            state.settings.thresholds,
            state._cells,
            state._index,
            state._reports,
            state.settings.report_policy,
            state._correlations,
        )
        state._msr_reports = {
            route.name: select_route_reports(
                route,
                state._reports,
                as_of,
                state.settings.report_policy,
                state.settings.thresholds,
            )
            for route in state._msrs
        }

    def advance_activity(
        self,
        activity_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        state = self._state
        moment = as_of or datetime.now(UTC)
        current = self._activity_by_id(activity_id)
        if current is None:
            raise KeyError(activity_id)
        advanced = manual_advance(current, moment)
        state._activity_overrides[activity_id] = advanced
        return _activity_document(advanced)

    def _activity_by_id(self, activity_id: str) -> Activity | None:
        state = self._state
        if activity_id in state._activity_overrides:
            return state._activity_overrides[activity_id]
        for item in state._correlations:
            if item.activity.activity_id == activity_id:
                return item.activity
        return None


def _activity_document(activity: Activity) -> dict[str, object]:
    return {
        "id": activity.activity_id,
        "state": activity.state,
        "cell_id": activity.cell_id,
        "bmoa": activity.bmoa,
        "latitude": activity.latitude,
        "longitude": activity.longitude,
        "acceptance": activity.acceptance,
        "updated_at": activity.updated_at.isoformat(),
        "site_type": state_site_type(activity.state),
        "source_ids": list(activity.source_ids),
    }

