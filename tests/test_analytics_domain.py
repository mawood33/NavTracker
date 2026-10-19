from datetime import UTC, datetime, timedelta

from analytics.domain.aoi import derive_areas
from analytics.domain.behavior import detect_milling, detect_slowdowns, detect_stops
from analytics.domain.reference import Msr, Site
from analytics.domain.thresholds import AnalysisThresholds
from analytics.domain.tracks import GpsFix, dedupe_fixes

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fix(vehicle, minutes, lat, lon, speed=None, source="a"):
    return GpsFix(
        vehicle_id=vehicle,
        latitude=lat,
        longitude=lon,
        timestamp=T0 + timedelta(minutes=minutes),
        speed_mps=speed,
        source=source,
    )


def test_dedupe_collapses_the_same_vehicle_and_timestamp():
    first = _fix("v1", 0, 10, 20, speed=None, source="chat")
    second = _fix("v1", 0, 10, 20, speed=4, source="clairvoyant")
    other = _fix("v2", 0, 11, 21, speed=1, source="chat")
    result = dedupe_fixes([first, second, other])
    assert [item.vehicle_id for item in result] == ["v1", "v2"]
    assert result[0].speed_mps == 4
    assert result[0].source == "chat"


def test_dedupe_tolerance_buckets_near_timestamps():
    result = dedupe_fixes(
        [_fix("v1", 0, 1, 2), _fix("v1", 0.4, 1, 2, source="b")],
        tolerance_seconds=60,
    )
    assert len(result) == 1


def test_slowdown_requires_a_sharp_drop():
    fixes = [
        _fix("v1", 0, 10, 20, speed=20),
        _fix("v1", 1, 10, 20, speed=8),
        _fix("v1", 2, 10, 20, speed=7),
    ]
    events = detect_slowdowns(fixes, AnalysisThresholds(slow_speed_mps=10, slowdown_ratio=0.5))
    assert len(events) == 1
    assert events[0].speed_mps == 8
    assert events[0].previous_speed_mps == 20


def test_stop_reports_dwell_and_ignores_a_short_pause():
    thresholds = AnalysisThresholds(stop_speed_mps=1, min_dwell_seconds=120)
    moving = [
        _fix("v1", 0, 10, 20, speed=10),
        _fix("v1", 1, 10.01, 20, speed=0),
        _fix("v1", 2, 10.01, 20, speed=0),
    ]
    assert detect_stops(moving, thresholds) == []
    stopped = [
        _fix("v1", 0, 10, 20, speed=0),
        _fix("v1", 3, 10, 20, speed=0),
    ]
    stops = detect_stops(stopped, thresholds)
    assert len(stops) == 1
    assert stops[0].dwell_seconds == 180
    assert stops[0].latitude == 10


def test_milling_needs_two_vehicles_nearby():
    thresholds = AnalysisThresholds(
        mill_radius_meters=200,
        mill_min_vehicles=2,
        mill_min_overlap_seconds=60,
    )
    stops = detect_stops(
        [
            _fix("a", 0, 36.0, 128.0, speed=0),
            _fix("a", 5, 36.0, 128.0, speed=0),
            _fix("b", 1, 36.001, 128.0, speed=0),
            _fix("b", 5, 36.001, 128.0, speed=0),
        ],
        AnalysisThresholds(min_dwell_seconds=60, stop_speed_mps=1),
    )
    mills = detect_milling(stops, thresholds)
    assert len(mills) == 1
    assert set(mills[0].vehicle_ids) == {"a", "b"}

    far = detect_stops(
        [
            _fix("a", 0, 36.0, 128.0, speed=0),
            _fix("a", 5, 36.0, 128.0, speed=0),
            _fix("b", 1, 36.05, 128.0, speed=0),
            _fix("b", 5, 36.05, 128.0, speed=0),
        ],
        AnalysisThresholds(min_dwell_seconds=60, stop_speed_mps=1),
    )
    assert detect_milling(far, thresholds) == []


def test_area_classification_uses_dwell_msr_and_known_sites():
    long_stop = detect_stops(
        [
            _fix("h", 0, 10, 20, speed=0),
            _fix("h", 40, 10, 20, speed=0),
        ],
        AnalysisThresholds(min_dwell_seconds=60),
    )
    hide = derive_areas(long_stop, [], [], AnalysisThresholds())
    assert hide[0].classification == "hide"
    assert hide[0].supported_types == frozenset({"hide"})
    assert hide[0].matched_msr is None

    route = Msr(name="MSR RED", points=((10.0, 20.0), (10.0, 20.01)))
    transload_stops = detect_stops(
        [
            _fix("a", 0, 10, 20, speed=0),
            _fix("a", 10, 10, 20, speed=0),
            _fix("b", 0, 10.0005, 20, speed=0),
            _fix("b", 10, 10.0005, 20, speed=0),
        ],
        AnalysisThresholds(min_dwell_seconds=60),
    )
    transload = derive_areas(
        transload_stops,
        [],
        [route],
        AnalysisThresholds(msr_corridor_meters=200, transload_min_dwell_seconds=60),
    )
    assert transload[0].classification == "transload"
    assert transload[0].matched_msr == "MSR RED"

    launches = []
    for index in range(3):
        start = index * 120
        launches.extend(
            [
                _fix("l", start, 11, 21, speed=0),
                _fix("l", start + 2, 11, 21, speed=0),
                _fix("l", start + 3, 12, 22, speed=20),
            ]
        )
    launch_stops = detect_stops(
        launches,
        AnalysisThresholds(min_dwell_seconds=60),
    )
    areas = derive_areas(
        launch_stops,
        [],
        [],
        AnalysisThresholds(
            launch_min_visits=3,
            launch_min_dwell_seconds=60,
            launch_max_dwell_seconds=600,
            revisit_gap_seconds=3600,
            hide_min_dwell_seconds=10_000,
        ),
    )
    assert areas[0].classification == "launch"
    assert areas[0].visit_count == 3

    known = Site(name="Pad", site_types=frozenset({"launch"}), latitude=10, longitude=20)
    overridden = derive_areas(
        long_stop,
        [known],
        [],
        AnalysisThresholds(site_match_meters=50),
    )
    assert overridden[0].supported_types == frozenset({"hide", "launch"})
    assert overridden[0].classification == "hide,launch"
    assert overridden[0].matched_site == "Pad"
