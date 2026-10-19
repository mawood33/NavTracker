import math

EARTH_RADIUS_METERS = 6_371_000.0


def haversine_meters(
    latitude: float,
    longitude: float,
    other_latitude: float,
    other_longitude: float,
) -> float:
    """Great-circle distance in meters."""

    phi1 = math.radians(latitude)
    phi2 = math.radians(other_latitude)
    d_phi = math.radians(other_latitude - latitude)
    d_lambda = math.radians(other_longitude - longitude)
    chord = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.atan2(
        math.sqrt(chord),
        math.sqrt(1 - chord),
    )


def distance_to_segment_meters(
    latitude: float,
    longitude: float,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> float:
    """Distance from a point to a segment, in a local meter plane."""

    origin_lat = latitude
    origin_lon = longitude
    px, py = 0.0, 0.0
    ax, ay = _local_meters(
        start_latitude, start_longitude, origin_lat, origin_lon
    )
    bx, by = _local_meters(
        end_latitude, end_longitude, origin_lat, origin_lon
    )
    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    raw = ((px - ax) * dx + (py - ay) * dy) / length_sq
    if raw < 0:
        ratio = 0.0
    elif raw > 1:
        ratio = 1.0
    else:
        ratio = raw
    closest_x = ax + ratio * dx
    closest_y = ay + ratio * dy
    return math.hypot(px - closest_x, py - closest_y)


def distance_to_polyline_meters(
    latitude: float,
    longitude: float,
    points: tuple[tuple[float, float], ...],
) -> float:
    """Distance to the nearest segment. A single point uses that point."""

    if not points:
        return math.inf
    if len(points) == 1:
        return haversine_meters(
            latitude, longitude, points[0][0], points[0][1]
        )
    best = math.inf
    for start, end in zip(points, points[1:], strict=False):
        distance = distance_to_segment_meters(
            latitude,
            longitude,
            start[0],
            start[1],
            end[0],
            end[1],
        )
        if distance < best:
            best = distance
    return best


def closest_point_on_polyline(
    latitude: float,
    longitude: float,
    points: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, int] | None:
    """Return the closest point, its distance, and the segment index."""

    if len(points) < 2:
        return None
    best_distance = math.inf
    best_point = points[0]
    best_index = 0
    for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
        point, distance = _closest_on_segment(
            latitude, longitude, start[0], start[1], end[0], end[1]
        )
        if distance < best_distance:
            best_distance = distance
            best_point = point
            best_index = index
    return best_point[0], best_point[1], best_distance, best_index


def _closest_on_segment(
    latitude: float,
    longitude: float,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> tuple[tuple[float, float], float]:
    ax, ay = _local_meters(
        start_latitude, start_longitude, latitude, longitude
    )
    bx, by = _local_meters(end_latitude, end_longitude, latitude, longitude)
    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return (start_latitude, start_longitude), math.hypot(ax, ay)
    raw = ((0.0 - ax) * dx + (0.0 - ay) * dy) / length_sq
    ratio = 0.0 if raw < 0 else 1.0 if raw > 1 else raw
    east = ax + ratio * dx
    north = ay + ratio * dy
    point = _unproject(east, north, latitude, longitude)
    return point, math.hypot(east, north)


def point_in_polygon(
    latitude: float,
    longitude: float,
    ring: tuple[tuple[float, float], ...],
) -> bool:
    """Ray cast. Ring points are (latitude, longitude)."""

    if len(ring) < 3:
        return False
    inside = False
    previous_lat, previous_lon = ring[-1]
    for current_lat, current_lon in ring:
        crosses = (current_lon > longitude) != (previous_lon > longitude)
        if crosses:
            latitude_at = (previous_lat - current_lat) * (
                longitude - current_lon
            ) / (previous_lon - current_lon) + current_lat
            if latitude < latitude_at:
                inside = not inside
        previous_lat, previous_lon = current_lat, current_lon
    return inside


def corridor_polygon(
    points: tuple[tuple[float, float], ...],
    buffer_meters: float,
) -> tuple[tuple[float, float], ...]:
    """Offset a polyline into a closed ring, in a local meter plane.

    Points are ``(latitude, longitude)``. The ring is the left side of
    the route followed by the right side, so it can be drawn as an MSR
    corridor of width ``2 * buffer_meters``.
    """

    if len(points) < 2 or buffer_meters <= 0:
        return ()
    origin_lat, origin_lon = points[0]
    local = [
        _local_meters(latitude, longitude, origin_lat, origin_lon)
        for latitude, longitude in points
    ]
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    for index, (east, north) in enumerate(local):
        normal = _vertex_normal(local, index)
        if normal is None:
            continue
        nx, ny = normal
        left.append((east + nx * buffer_meters, north + ny * buffer_meters))
        right.append((east - nx * buffer_meters, north - ny * buffer_meters))
    if len(left) < 2 or len(right) < 2:
        return ()
    ring = left + list(reversed(right))
    return tuple(
        _unproject(east, north, origin_lat, origin_lon) for east, north in ring
    )


def _vertex_normal(
    local: list[tuple[float, float]],
    index: int,
) -> tuple[float, float] | None:
    if index == 0:
        return _unit_left(
            local[1][0] - local[0][0],
            local[1][1] - local[0][1],
        )
    if index == len(local) - 1:
        return _unit_left(
            local[index][0] - local[index - 1][0],
            local[index][1] - local[index - 1][1],
        )
    before = _unit_left(
        local[index][0] - local[index - 1][0],
        local[index][1] - local[index - 1][1],
    )
    after = _unit_left(
        local[index + 1][0] - local[index][0],
        local[index + 1][1] - local[index][1],
    )
    if before is None:
        return after
    if after is None:
        return before
    nx = before[0] + after[0]
    ny = before[1] + after[1]
    length = math.hypot(nx, ny)
    if length < 1e-9:
        return before
    return nx / length, ny / length


def _unit_left(east: float, north: float) -> tuple[float, float] | None:
    length = math.hypot(east, north)
    if length == 0:
        return None
    return -north / length, east / length


def _unproject(
    east: float,
    north: float,
    origin_latitude: float,
    origin_longitude: float,
) -> tuple[float, float]:
    latitude = origin_latitude + math.degrees(north / EARTH_RADIUS_METERS)
    cosine = math.cos(math.radians(origin_latitude)) or 1e-12
    longitude = origin_longitude + math.degrees(
        east / (EARTH_RADIUS_METERS * cosine)
    )
    return latitude, longitude


def _local_meters(
    latitude: float,
    longitude: float,
    origin_latitude: float,
    origin_longitude: float,
) -> tuple[float, float]:
    east = (
        math.radians(longitude - origin_longitude)
        * math.cos(math.radians(origin_latitude))
        * EARTH_RADIUS_METERS
    )
    north = math.radians(latitude - origin_latitude) * EARTH_RADIUS_METERS
    return east, north
