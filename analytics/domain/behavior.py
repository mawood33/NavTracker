from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from analytics.domain.geo import haversine_meters
from analytics.domain.thresholds import AnalysisThresholds
from analytics.domain.tracks import GpsFix, seconds_between


@dataclass(frozen=True)
class Slowdown:
    vehicle_id: str
    timestamp: datetime
    latitude: float
    longitude: float
    previous_speed_mps: float
    speed_mps: float


@dataclass(frozen=True)
class Stop:
    vehicle_id: str
    start: datetime
    end: datetime
    dwell_seconds: float
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Mill:
    vehicle_ids: tuple[str, ...]
    start: datetime
    end: datetime
    dwell_seconds: float
    latitude: float
    longitude: float


def detect_slowdowns(
    fixes: Sequence[GpsFix],
    thresholds: AnalysisThresholds,
) -> list[Slowdown]:
    """Report a sharp speed drop that crosses below ``slow_speed_mps``."""

    events: list[Slowdown] = []
    for vehicle_id, track in _by_vehicle(fixes).items():
        for previous, current in zip(track, track[1:], strict=False):
            before = _speed(previous, None)
            after = _speed(current, previous)
            if before is None or after is None:
                continue
            if before <= thresholds.slow_speed_mps:
                continue
            if after > before * thresholds.slowdown_ratio:
                continue
            events.append(
                Slowdown(
                    vehicle_id=vehicle_id,
                    timestamp=current.timestamp,
                    latitude=current.latitude,
                    longitude=current.longitude,
                    previous_speed_mps=before,
                    speed_mps=after,
                )
            )
    return events


def detect_stops(
    fixes: Sequence[GpsFix],
    thresholds: AnalysisThresholds,
) -> list[Stop]:
    """Report contiguous slow periods that last long enough to be a stop."""

    events: list[Stop] = []
    for vehicle_id, track in _by_vehicle(fixes).items():
        run: list[GpsFix] = []
        previous: GpsFix | None = None
        for fix in track:
            speed = _speed(fix, previous)
            stopped = speed is not None and speed <= thresholds.stop_speed_mps
            if stopped:
                run.append(fix)
            elif run:
                stop = _close_stop(vehicle_id, run, thresholds.min_dwell_seconds)
                if stop is not None:
                    events.append(stop)
                run = []
            previous = fix
        if run:
            stop = _close_stop(vehicle_id, run, thresholds.min_dwell_seconds)
            if stop is not None:
                events.append(stop)
    return events


def detect_milling(
    stops: Sequence[Stop],
    thresholds: AnalysisThresholds,
) -> list[Mill]:
    """Group nearby, overlapping stops from several vehicles."""

    parent = list(range(len(stops)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        parent[find(left)] = find(right)

    for index, stop in enumerate(stops):
        for other_index in range(index + 1, len(stops)):
            other = stops[other_index]
            if stop.vehicle_id == other.vehicle_id:
                continue
            overlap = _overlap_seconds(stop, other)
            if overlap < thresholds.mill_min_overlap_seconds:
                continue
            distance = haversine_meters(
                stop.latitude,
                stop.longitude,
                other.latitude,
                other.longitude,
            )
            if distance <= thresholds.mill_radius_meters:
                union(index, other_index)

    groups: dict[int, list[Stop]] = {}
    for index, stop in enumerate(stops):
        groups.setdefault(find(index), []).append(stop)

    mills: list[Mill] = []
    for group in groups.values():
        vehicles = tuple(dict.fromkeys(stop.vehicle_id for stop in group))
        if len(vehicles) < thresholds.mill_min_vehicles:
            continue
        start = min(stop.start for stop in group)
        end = max(stop.end for stop in group)
        dwell = max(
            _overlap_seconds(left, right)
            for left in group
            for right in group
            if left.vehicle_id != right.vehicle_id
        )
        mills.append(
            Mill(
                vehicle_ids=vehicles,
                start=start,
                end=end,
                dwell_seconds=dwell,
                latitude=sum(stop.latitude for stop in group) / len(group),
                longitude=sum(stop.longitude for stop in group) / len(group),
            )
        )
    return mills


def _by_vehicle(fixes: Sequence[GpsFix]) -> dict[str, list[GpsFix]]:
    grouped: dict[str, list[GpsFix]] = {}
    for fix in sorted(fixes, key=lambda item: (item.vehicle_id, item.timestamp)):
        grouped.setdefault(fix.vehicle_id, []).append(fix)
    return grouped


def _speed(current: GpsFix, previous: GpsFix | None) -> float | None:
    if current.speed_mps is not None:
        return current.speed_mps
    if previous is None:
        return None
    elapsed = seconds_between(previous.timestamp, current.timestamp)
    if elapsed <= 0:
        return 0.0
    distance = haversine_meters(
        previous.latitude,
        previous.longitude,
        current.latitude,
        current.longitude,
    )
    return distance / elapsed


def _close_stop(
    vehicle_id: str,
    run: list[GpsFix],
    min_dwell_seconds: float,
) -> Stop | None:
    start = run[0].timestamp
    end = run[-1].timestamp
    dwell = seconds_between(start, end)
    if dwell < min_dwell_seconds:
        return None
    return Stop(
        vehicle_id=vehicle_id,
        start=start,
        end=end,
        dwell_seconds=dwell,
        latitude=sum(fix.latitude for fix in run) / len(run),
        longitude=sum(fix.longitude for fix in run) / len(run),
    )


def _overlap_seconds(left: Stop, right: Stop) -> float:
    start = left.start if left.start > right.start else right.start
    end = left.end if left.end < right.end else right.end
    return max(0.0, seconds_between(start, end))
