"""Google Maps Platform Routes API (computeRouteMatrix) travel provider.

* Enabled only when ``GOOGLE_MAPS_API_KEY`` is set. Never commit keys.
* Uses the current Routes API (``routes.googleapis.com/distanceMatrix/v2``),
  not the legacy Distance Matrix API.
* The full location x location matrix is requested in 25x25 blocks
  (625 elements = the per-request limit for TRAFFIC_AWARE), *once*, and
  cached on disk. The solver only ever reads the cached matrix.
* Any element (or whole request) that fails falls back to the synthetic
  estimate, and the matrix source is labelled accordingly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from ..domain import Location
from .base import BaseMatrix, TravelTimeProvider
from .synthetic import SyntheticTravelTimeProvider

log = logging.getLogger(__name__)

ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
BLOCK = 25


class GoogleRoutesTravelTimeProvider(TravelTimeProvider):
    name = "google-routes"

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        fallback: TravelTimeProvider | None = None,
        timeout_s: float = 20.0,
        max_requests: int = 400,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
        if not self.api_key:
            raise RuntimeError("GOOGLE_MAPS_API_KEY is not set")
        self.cache_dir = Path(cache_dir or os.environ.get("NOTANA_CACHE_DIR", "data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fallback = fallback or SyntheticTravelTimeProvider()
        self.timeout_s = timeout_s
        self.max_requests = max_requests

    # -- caching -------------------------------------------------------------
    def _cache_key(self, locations: list[Location], departure_minute: int) -> str:
        coords = "|".join(f"{loc.lat:.5f},{loc.lon:.5f}" for loc in locations)
        bucket = departure_minute // 60  # one matrix per departure hour
        return hashlib.sha256(f"{coords}#{bucket}".encode()).hexdigest()[:24]

    def build_matrix(self, locations: list[Location], departure_minute: int = 8 * 60) -> BaseMatrix:
        key = self._cache_key(locations, departure_minute)
        path = self.cache_dir / f"google_matrix_{key}.json"
        if path.exists():
            data = json.loads(path.read_text())
            return BaseMatrix(
                location_ids=[loc.id for loc in locations],
                minutes=data["minutes"],
                km=data["km"],
                source=data["source"],
                notes=data.get("notes", []) + ["loaded from disk cache"],
            )

        synthetic = self.fallback.build_matrix(locations, departure_minute)
        n = len(locations)
        minutes = [row[:] for row in synthetic.minutes]
        km = [row[:] for row in synthetic.km]
        filled = 0
        failed = 0
        requests = 0
        departure = self._departure_time(departure_minute)
        for oi in range(0, n, BLOCK):
            for di in range(0, n, BLOCK):
                if requests >= self.max_requests:
                    failed += 1
                    continue
                requests += 1
                origins = locations[oi : oi + BLOCK]
                dests = locations[di : di + BLOCK]
                try:
                    for el in self._request(origins, dests, departure):
                        o = oi + el.get("originIndex", 0)
                        d = di + el.get("destinationIndex", 0)
                        if o == d or el.get("condition") != "ROUTE_EXISTS":
                            continue
                        dur = el.get("duration")
                        if not dur:
                            continue
                        # Add the same parking / door overhead the synthetic model uses.
                        minutes[o][d] = float(dur.rstrip("s")) / 60.0 + self.fallback.config.per_trip_overhead_min  # type: ignore[attr-defined]
                        km[o][d] = el.get("distanceMeters", 0) / 1000.0
                        filled += 1
                except Exception as exc:  # noqa: BLE001 - any failure -> fallback
                    failed += 1
                    log.warning("Route matrix block failed (%s); using synthetic fallback", exc)

        total = n * (n - 1)
        if filled == 0:
            source = "synthetic (google-routes unavailable)"
        elif filled < total:
            source = "google-routes+synthetic-fallback"
        else:
            source = "google-routes"
        result = BaseMatrix(
            location_ids=[loc.id for loc in locations],
            minutes=minutes,
            km=km,
            source=source,
            notes=[
                f"{filled}/{total} elements from Routes API in {requests} requests; "
                f"{failed} failed blocks fell back to synthetic"
            ],
        )
        if filled > 0:
            path.write_text(
                json.dumps({"minutes": minutes, "km": km, "source": source, "notes": result.notes})
            )
        return result

    @staticmethod
    def _departure_time(departure_minute: int) -> str:
        """Next future occurrence of the local (Europe/Stockholm) time of day, as RFC 3339 UTC.

        Traffic-aware routing requires a departure time in the future.
        """
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(os.environ.get("NOTANA_TIMEZONE", "Europe/Stockholm"))
        now = dt.datetime.now(tz)
        target = now.replace(hour=departure_minute // 60, minute=departure_minute % 60, second=0, microsecond=0)
        if target <= now + dt.timedelta(minutes=5):
            target += dt.timedelta(days=1)
        return target.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _request(self, origins: list[Location], dests: list[Location], departure: str) -> list[dict]:
        def wp(loc: Location) -> dict:
            return {"waypoint": {"location": {"latLng": {"latitude": loc.lat, "longitude": loc.lon}}}}

        body = {
            "origins": [wp(o) for o in origins],
            "destinations": [wp(d) for d in dests],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "departureTime": departure,
        }
        req = urllib.request.Request(
            ROUTE_MATRIX_URL,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key or "",
                "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,condition",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - fixed https URL
            data = json.loads(resp.read().decode())
        if isinstance(data, dict):  # error payload
            raise RuntimeError(data.get("error", {}).get("message", str(data)))
        return data
