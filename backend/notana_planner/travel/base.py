"""Travel-time provider abstraction and the precomputed travel matrix.

The solver never calls a provider directly. A provider produces a *base*
location-to-location matrix once (cached), and :class:`TravelMatrix` applies
the currently active traffic conditions (global / zone / corridor
multipliers) on top of it. Both the optimizer and the independent validator
read travel times from the same :class:`TravelMatrix`, which is the single
source of truth for "how long does it take to get from A to B right now".
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..domain import Location


@dataclass(slots=True)
class BaseMatrix:
    location_ids: list[str]
    minutes: list[list[float]]  # un-rounded travel minutes
    km: list[list[float]]
    source: str  # e.g. "synthetic", "google-routes", "google-routes+synthetic-fallback"
    notes: list[str] = field(default_factory=list)


class TravelTimeProvider(ABC):
    """Produces a base travel matrix for a set of locations."""

    name: str = "abstract"

    @abstractmethod
    def build_matrix(self, locations: list[Location], departure_minute: int = 8 * 60) -> BaseMatrix:
        raise NotImplementedError


@dataclass(slots=True)
class TrafficConditions:
    """Real-time travel modifiers layered on top of the base matrix."""

    global_multiplier: float = 1.0
    zone_multipliers: dict[str, float] = field(default_factory=dict)
    # Corridor multipliers keyed by "ZoneA|ZoneB" (order-insensitive).
    corridor_multipliers: dict[str, float] = field(default_factory=dict)

    def copy(self) -> "TrafficConditions":
        return TrafficConditions(
            self.global_multiplier, dict(self.zone_multipliers), dict(self.corridor_multipliers)
        )

    @staticmethod
    def corridor_key(a: str, b: str) -> str:
        return "|".join(sorted((a, b)))

    def factor(self, zone_a: str, zone_b: str) -> float:
        f = self.global_multiplier
        za = self.zone_multipliers.get(zone_a, 1.0)
        zb = self.zone_multipliers.get(zone_b, 1.0)
        # A trip touching a congested zone is slowed by the worse of the two.
        f *= max(za, zb)
        if zone_a != zone_b:
            f *= self.corridor_multipliers.get(self.corridor_key(zone_a, zone_b), 1.0)
        return f

    def to_dict(self) -> dict:
        return {
            "global_multiplier": self.global_multiplier,
            "zone_multipliers": dict(self.zone_multipliers),
            "corridor_multipliers": dict(self.corridor_multipliers),
        }


class TravelMatrix:
    """Integer-minute travel matrix with traffic applied. Travel is rounded *up*."""

    def __init__(
        self,
        base: BaseMatrix,
        locations: dict[str, Location],
        scenario_multiplier: float = 1.0,
        traffic: TrafficConditions | None = None,
    ) -> None:
        self.base = base
        self.locations = locations
        self.scenario_multiplier = scenario_multiplier
        self.traffic = traffic or TrafficConditions()
        self.index = {loc_id: i for i, loc_id in enumerate(base.location_ids)}
        self._minutes = self._compute()

    def _compute(self) -> list[list[int]]:
        ids = self.base.location_ids
        zones = [self.locations[i].zone for i in ids]
        n = len(ids)
        out = [[0] * n for _ in range(n)]
        for a in range(n):
            row_base = self.base.minutes[a]
            row = out[a]
            za = zones[a]
            for b in range(n):
                if a == b:
                    continue
                f = self.scenario_multiplier * self.traffic.factor(za, zones[b])
                row[b] = int(math.ceil(row_base[b] * f - 1e-9))
        return out

    @property
    def source(self) -> str:
        return self.base.source

    def with_traffic(self, traffic: TrafficConditions) -> "TravelMatrix":
        return TravelMatrix(self.base, self.locations, self.scenario_multiplier, traffic)

    def minutes(self, from_loc: str, to_loc: str) -> int:
        return self._minutes[self.index[from_loc]][self.index[to_loc]]

    def km(self, from_loc: str, to_loc: str) -> float:
        return self.base.km[self.index[from_loc]][self.index[to_loc]]

    def rows(self) -> list[list[int]]:
        return self._minutes
