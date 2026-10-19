"""Site-to-site routes. The route engine is injected."""

from analytics.application.state import AnalyticsState
from analytics.domain.routing import (
    METERS_PER_MILE,
    RoutingError,
    limit_speed,
    reject_unsupported_vehicle,
    route_document,
)


class RoutingService:
    def __init__(self, state: AnalyticsState) -> None:
        self._state = state

    def route_between(
        self,
        origin_name: str,
        destination_name: str,
        profile_name: str,
    ) -> dict[str, object]:
        state = self._state
        origin = state._named_site(origin_name)
        destination = state._named_site(destination_name)
        reject_unsupported_vehicle(profile_name)
        profile = limit_speed(state._named_profile(profile_name), None)
        if origin.latitude is None or origin.longitude is None:
            raise RoutingError(f"Site {origin.name} has no point.")
        if destination.latitude is None or destination.longitude is None:
            raise RoutingError(f"Site {destination.name} has no point.")
        if origin.name == destination.name:
            raise RoutingError("Origin and destination are the same site.")
        leg = state._router.route(
            (origin.latitude, origin.longitude),
            (destination.latitude, destination.longitude),
            profile,
            state._msrs,
        )
        return route_document(leg, origin.name, destination.name)

    def api_route(
        self,
        origin_id: str,
        destination_id: str,
        vehicle_type: str,
        max_speed_mph: float | None,
    ) -> dict[str, object]:
        state = self._state
        reject_unsupported_vehicle(vehicle_type)
        profile = limit_speed(state._named_profile(vehicle_type), max_speed_mph)
        origin = state._named_site(origin_id)
        destination = state._named_site(destination_id)
        if origin.latitude is None or origin.longitude is None:
            raise RoutingError(f"Site {origin.name} has no point.", "missing_point")
        if destination.latitude is None or destination.longitude is None:
            raise RoutingError(
                f"Site {destination.name} has no point.",
                "missing_point",
            )
        if origin.name == destination.name:
            raise RoutingError(
                "Origin and destination are the same site.",
                "same_site",
            )
        leg = state._router.route(
            (origin.latitude, origin.longitude),
            (destination.latitude, destination.longitude),
            profile,
            state._msrs,
        )
        return {
            "provider": leg.engine,
            "routes": [
                {
                    "originSiteId": origin.name,
                    "destinationSiteId": destination.name,
                    "vehicleType": vehicle_type,
                    "provider": leg.engine,
                    "distanceMeters": leg.distance_meters,
                    "distanceMiles": leg.distance_meters / METERS_PER_MILE,
                    "durationMinutes": leg.duration_seconds / 60,
                    "geometry": route_document(leg, origin.name, destination.name)["geometry"],
                }
            ],
        }

