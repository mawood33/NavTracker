"""H3 adapter for ``analytics.domain.grid.CellIndex``.

Resolution is the H3 resolution (0 through 15). The domain scores cells
without importing this module, so a different index can replace it.
"""

import h3


class H3Index:
    def __init__(self, resolution: int):
        if resolution < 0 or resolution > 15:
            raise ValueError(f"H3 resolution must be 0..15, got {resolution}.")
        self.resolution = resolution

    def cell_id(self, latitude: float, longitude: float) -> str:
        return str(h3.latlng_to_cell(latitude, longitude, self.resolution))

    def center(self, cell_id: str) -> tuple[float, float]:
        latitude, longitude = h3.cell_to_latlng(cell_id)
        return float(latitude), float(longitude)

    def boundary(self, cell_id: str) -> tuple[tuple[float, float], ...]:
        ring: list[tuple[float, float]] = []
        for latitude, longitude in h3.cell_to_boundary(cell_id):
            ring.append((float(latitude), float(longitude)))
        return tuple(ring)
