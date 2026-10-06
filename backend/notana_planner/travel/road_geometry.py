"""Road geometry for drawing one employee's route on the map (display only).

The optimizer never uses this: it plans on the cached travel-time matrix.
With ``GOOGLE_MAPS_API_KEY`` set, the Routes API ``computeRoutes`` endpoint
returns the driving path of each leg (GeoJSON line strings). Results are
cached in memory per stop sequence. Without a key, or on any error, the
caller draws straight lines and says so.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from functools import lru_cache

log = logging.getLogger(__name__)

COMPUTE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MAX_INTERMEDIATES = 25


def _post(body: dict, key: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        COMPUTE_ROUTES_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": "routes.legs.polyline,routes.legs.duration,routes.legs.distanceMeters",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        return json.loads(resp.read().decode())


def _wp(lat: float, lon: float) -> dict:
    return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}


@lru_cache(maxsize=512)
def _legs_cached(points: tuple[tuple[float, float], ...], key: str) -> tuple[tuple, ...]:
    legs: list[tuple] = []
    # Chunk long routes (origin + 25 intermediates + destination per request).
    step = MAX_INTERMEDIATES + 1
    for i in range(0, len(points) - 1, step):
        chunk = points[i : i + step + 1]
        body = {
            "origin": _wp(*chunk[0]),
            "destination": _wp(*chunk[-1]),
            "intermediates": [_wp(*p) for p in chunk[1:-1]],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_UNAWARE",
            "polylineEncoding": "GEO_JSON_LINESTRING",
        }
        data = _post(body, key)
        route = (data.get("routes") or [{}])[0]
        for leg in route.get("legs", []):
            coords = ((leg.get("polyline") or {}).get("geoJsonLinestring") or {}).get("coordinates") or []
            path = tuple((c[1], c[0]) for c in coords)  # GeoJSON is [lng, lat]
            dur = float(str(leg.get("duration", "0s")).rstrip("s") or 0)
            legs.append((path, dur, int(leg.get("distanceMeters", 0))))
    return tuple(legs)


def road_legs(points: list[tuple[float, float]]) -> dict:
    """Legs between consecutive points: road paths if available, else straight lines."""
    straight = {
        "source": "straight",
        "legs": [{"path": [list(a), list(b)], "duration_s": None, "distance_m": None} for a, b in zip(points, points[1:])],
    }
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key or len(points) < 2:
        return straight
    # Consecutive identical points (e.g. office -> break at the office) need no request.
    distinct = [points[0]] + [b for a, b in zip(points, points[1:]) if a != b]
    if len(distinct) < 2:
        return straight
    try:
        legs = list(_legs_cached(tuple(distinct), key))
    except Exception as exc:  # noqa: BLE001 - display only, fall back
        log.warning("computeRoutes failed (%s); drawing straight lines", exc)
        return straight | {"error": type(exc).__name__}
    if len(legs) != len(distinct) - 1:
        return straight | {"error": "leg count mismatch"}
    out = []
    for a, b in zip(points, points[1:]):
        if a == b:
            out.append({"path": [list(a)], "duration_s": 0, "distance_m": 0})
            continue
        path, dur, dist = legs.pop(0)
        out.append({"path": [list(p) for p in path] or [list(a), list(b)], "duration_s": dur, "distance_m": dist})
    return {"source": "google-routes", "legs": out}
