from datetime import UTC, datetime, timedelta

from analytics.domain.assessment import (
    ConfidencePolicy,
    SiteActivity,
    assess_site,
    stamp_import,
    update_sites,
)
from analytics.domain.grid import CellActivity
from analytics.domain.reference import Site
from analytics.domain.thresholds import AnalysisThresholds

T0 = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = ConfidencePolicy()


def _site(types: frozenset[str] = frozenset()) -> Site:
    return Site(name="06001", site_types=types, latitude=10.0, longitude=20.0)


def _activity(**overrides: object) -> SiteActivity:
    values: dict[str, object] = {
        "as_of": T0,
        "stops": 0,
        "dwell_seconds": 0.0,
        "distinct_vehicles": 0,
        "revisits": 0,
        "milling": False,
        "on_msr": False,
        "long_dwell": False,
        "cell_id": "",
        "cell_score": 0.0,
        "cell_hotspot": False,
        "reports": (),
        "last_activity_at": None,
    }
    values.update(overrides)
    return SiteActivity(**values)  # type: ignore[arg-type]


def test_import_starts_possible_with_import_evidence():
    site = stamp_import(_site(), POLICY)
    assert site.status == "possible"
    assert site.confidence == POLICY.initial_confidence
    assert site.origin == "import"
    assert any(item.kind == "import" for item in site.evidence)


def test_repeated_assessment_does_not_stack():
    imported = stamp_import(_site(), POLICY)
    activity = _activity(stops=1, dwell_seconds=600)
    once = assess_site(imported, activity, POLICY)
    twice = assess_site(once, activity, POLICY)
    assert twice.confidence == once.confidence
    assert twice.status == once.status
    assert [item.kind for item in twice.evidence].count("stop") == 1


def test_declared_type_is_kept():
    imported = stamp_import(_site(frozenset({"launch"})), POLICY)
    updated = assess_site(imported, _activity(), POLICY)
    assert "launch" in updated.site_types


def test_type_evidence_for_mill_hide_and_revisits():
    imported = stamp_import(_site(), POLICY)
    milled = assess_site(imported, _activity(milling=True), POLICY)
    assert "transload" in milled.site_types
    hidden = assess_site(imported, _activity(long_dwell=True), POLICY)
    assert "hide" in hidden.site_types
    one = assess_site(imported, _activity(revisits=1), POLICY)
    assert "launch" not in one.site_types
    two = assess_site(imported, _activity(revisits=2), POLICY)
    assert "launch" in two.site_types


def test_msr_adjacency_does_not_add_a_type_by_itself():
    updated = assess_site(stamp_import(_site(), POLICY), _activity(on_msr=True), POLICY)
    assert "transload" not in updated.site_types
    assert any(item.kind == "msr_adjacency" for item in updated.evidence)


def test_activity_can_raise_status_and_a_quiet_window_lowers_it():
    busy = _activity(stops=1, dwell_seconds=600, milling=True, distinct_vehicles=2)
    raised = assess_site(stamp_import(_site(), POLICY), busy, POLICY)
    assert raised.status == "probable"
    quiet = assess_site(raised, _activity(), POLICY)
    assert quiet.status == "possible"
    assert quiet.confidence == POLICY.initial_confidence


def test_decay_is_off_by_default_and_can_lower_confidence():
    idle = _activity(last_activity_at=T0 - timedelta(days=5))
    untouched = assess_site(stamp_import(_site(), POLICY), idle, POLICY)
    assert untouched.confidence == POLICY.initial_confidence
    assert not any(item.kind == "decay" for item in untouched.evidence)
    decaying = ConfidencePolicy(decay_per_day=0.1)
    dropped = assess_site(stamp_import(_site(), POLICY), idle, decaying)
    assert any(item.kind == "decay" and item.weight < 0 for item in dropped.evidence)
    assert dropped.confidence < POLICY.initial_confidence


def test_unmatched_hotspot_becomes_a_possible_site():
    cell = CellActivity(
        cell_id="abc",
        latitude=10.0,
        longitude=20.0,
        boundary=((10.0, 20.0), (10.1, 20.0), (10.0, 20.1)),
        score=2.0,
        stops=1,
        dwell_seconds=100.0,
        vehicle_ids=("v1",),
        revisits=0,
        milling=False,
        on_msr=False,
        long_dwell=False,
        hotspot=True,
    )
    sites = update_sites(
        (),
        (),
        (),
        (),
        (),
        T0,
        POLICY,
        AnalysisThresholds(),
        cells=(cell,),
    )
    assert sites[0].name == "cell-abc"
    assert sites[0].origin == "discovered"
    assert sites[0].status == "possible"
