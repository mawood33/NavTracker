"""Reference-file column aliases. Matching is case-insensitive."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnMap:
    """Header aliases for Player Data CSV and XLSX sheets."""

    name: tuple[str, ...] = ("name/id", "name", "id", "site")
    site_type: tuple[str, ...] = ("type", "site type", "site_type")
    latitude_dms: tuple[str, ...] = ("dms lat", "latitude dms")
    longitude_dms: tuple[str, ...] = ("dms long", "dms lon", "longitude dms")
    latitude: tuple[str, ...] = ("lat", "latitude")
    longitude: tuple[str, ...] = ("lon", "long", "longitude")
    mgrs: tuple[str, ...] = ("mgrs",)

    def override(self, field: str, header: str) -> "ColumnMap":
        text = header.strip().lower()
        current = {
            "name": self.name,
            "site_type": self.site_type,
            "latitude_dms": self.latitude_dms,
            "longitude_dms": self.longitude_dms,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "mgrs": self.mgrs,
        }
        if field not in current:
            raise ValueError(f"Unknown reference column {field}")
        current[field] = (text,)
        return ColumnMap(**current)
