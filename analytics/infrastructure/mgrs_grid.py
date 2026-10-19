"""MGRS adapter. The domain asks this port for a point and does not import the library."""

from analytics.domain.timeutil import valid_position


class MgrsLibrary:
    """Convert MGRS with the ``mgrs`` package. Invalid strings yield None."""

    def to_latlon(self, text: str) -> tuple[float, float] | None:
        cleaned = "".join(text.split()).upper()
        if not cleaned:
            return None
        try:
            import mgrs
        except ImportError:
            return None
        try:
            latitude, longitude = mgrs.MGRS().toLatLon(cleaned)
        except Exception:
            return None
        point = (float(latitude), float(longitude))
        if not valid_position(point[0], point[1]):
            return None
        return point

    def to_mgrs(self, latitude: float, longitude: float, precision_m: int) -> str | None:
        digits = {1: 5, 10: 4, 100: 3}.get(precision_m)
        if digits is None or not valid_position(latitude, longitude):
            return None
        try:
            import mgrs
        except ImportError:
            return None
        try:
            text = mgrs.MGRS().toMGRS(float(latitude), float(longitude), MGRSPrecision=digits)
        except Exception:
            return None
        compact = "".join(str(text).split()).upper()
        return compact or None
