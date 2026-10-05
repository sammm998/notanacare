"""Synthetic travel times: Haversine distance x routing detour / urban speed."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import Location
from ..geo import haversine_km
from .base import BaseMatrix, TravelTimeProvider


@dataclass(slots=True)
class SyntheticTravelConfig:
    # Average door-to-door speed of a home-care car trip in a Swedish city,
    # including traffic lights and congestion, but excluding parking.
    urban_speed_kmh: float = 24.0
    # Road distance is longer than great-circle distance.
    routing_factor: float = 1.35
    # Fixed per-trip overhead: parking, walking to the door, stairs/elevator.
    per_trip_overhead_min: float = 3.0


class SyntheticTravelTimeProvider(TravelTimeProvider):
    name = "synthetic"

    def __init__(self, config: SyntheticTravelConfig | None = None) -> None:
        self.config = config or SyntheticTravelConfig()

    def trip(self, a: Location, b: Location) -> tuple[float, float]:
        """Return (minutes, road_km) for a single trip."""
        if a.id == b.id:
            return 0.0, 0.0
        km = haversine_km(a.lat, a.lon, b.lat, b.lon) * self.config.routing_factor
        if km < 0.05:  # same building
            return 1.0, km
        minutes = self.config.per_trip_overhead_min + km / self.config.urban_speed_kmh * 60.0
        return minutes, km

    def build_matrix(self, locations: list[Location], departure_minute: int = 8 * 60) -> BaseMatrix:
        n = len(locations)
        minutes = [[0.0] * n for _ in range(n)]
        km = [[0.0] * n for _ in range(n)]
        for i, a in enumerate(locations):
            for j, b in enumerate(locations):
                if i != j:
                    minutes[i][j], km[i][j] = self.trip(a, b)
        return BaseMatrix(
            location_ids=[loc.id for loc in locations],
            minutes=minutes,
            km=km,
            source="synthetic",
            notes=[
                f"Haversine x {self.config.routing_factor} routing factor at "
                f"{self.config.urban_speed_kmh} km/h + {self.config.per_trip_overhead_min} min/trip overhead"
            ],
        )
