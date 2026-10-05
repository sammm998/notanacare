"""Geography helpers and operating-area presets (synthetic but plausible)."""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True, slots=True)
class Cluster:
    name: str
    lat: float
    lon: float
    radius_km: float
    weight: float  # relative share of recipients


@dataclass(frozen=True, slots=True)
class AreaPreset:
    key: str
    name: str
    center: tuple[float, float]
    clusters: tuple[Cluster, ...]
    street_names: tuple[str, ...]


AREAS: dict[str, AreaPreset] = {
    "stockholm": AreaPreset(
        key="stockholm",
        name="Stockholm",
        center=(59.325, 18.03),
        clusters=(
            Cluster("Södermalm", 59.3150, 18.0710, 1.1, 1.3),
            Cluster("Vasastan", 59.3445, 18.0450, 0.9, 1.0),
            Cluster("Kungsholmen", 59.3320, 18.0300, 0.8, 0.8),
            Cluster("Östermalm", 59.3390, 18.0880, 0.9, 0.9),
            Cluster("Bromma", 59.3380, 17.9400, 1.6, 0.9),
            Cluster("Hägersten", 59.3010, 17.9850, 1.3, 1.0),
            Cluster("Farsta", 59.2440, 18.0900, 1.3, 0.8),
        ),
        street_names=(
            "Götgatan", "Hornsgatan", "Folkungagatan", "Odengatan", "Sankt Eriksgatan",
            "Fleminggatan", "Hantverkargatan", "Karlavägen", "Strandvägen", "Valhallavägen",
            "Drottningholmsvägen", "Brommaplan", "Hägerstensvägen", "Västertorpsvägen",
            "Farsta Strandväg", "Larsbodavägen", "Ringvägen", "Torsgatan", "Upplandsgatan",
            "Sveavägen", "Birger Jarlsgatan", "Sandhamnsgatan", "Tegnérgatan", "Bellmansgatan",
        ),
    ),
    "goteborg": AreaPreset(
        key="goteborg",
        name="Göteborg",
        center=(57.705, 11.97),
        clusters=(
            Cluster("Majorna", 57.6940, 11.9200, 1.0, 1.0),
            Cluster("Centrum", 57.7060, 11.9700, 0.9, 1.0),
            Cluster("Hisingen", 57.7300, 11.9400, 1.6, 1.0),
            Cluster("Örgryte", 57.7000, 12.0100, 1.1, 0.8),
            Cluster("Frölunda", 57.6550, 11.9100, 1.3, 0.9),
        ),
        street_names=(
            "Linnégatan", "Kungsgatan", "Vasagatan", "Aschebergsgatan", "Mölndalsvägen",
            "Långgatan", "Karl Johansgatan", "Hisingsgatan", "Frölundagatan", "Sankt Pauligatan",
        ),
    ),
    "uppsala": AreaPreset(
        key="uppsala",
        name="Uppsala",
        center=(59.858, 17.64),
        clusters=(
            Cluster("Centrum", 59.8586, 17.6389, 0.9, 1.0),
            Cluster("Luthagen", 59.8650, 17.6200, 0.8, 0.8),
            Cluster("Gottsunda", 59.8100, 17.6200, 1.1, 0.9),
            Cluster("Sävja", 59.8200, 17.7000, 1.0, 0.7),
            Cluster("Gränby", 59.8800, 17.6800, 1.0, 0.8),
        ),
        street_names=(
            "Svartbäcksgatan", "Kungsgatan", "Sysslomansgatan", "Väderkvarnsgatan",
            "Gottsundavägen", "Sävja Allé", "Gränbyvägen", "Luthagsesplanaden",
        ),
    ),
}


def get_area(key: str) -> AreaPreset:
    try:
        return AREAS[key.lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown area '{key}'. Available: {sorted(AREAS)}") from exc
