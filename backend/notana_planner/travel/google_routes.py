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
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..domain import Location
from .base import BaseMatrix, TravelTimeProvider
from .synthetic import SyntheticTravelTimeProvider

log = logging.getLogger(__name__)

ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
BLOCK = 25


class _FatalRouteError(RuntimeError):
    """Key / permission / request errors: retrying will not help."""


class GoogleRoutesTravelTimeProvider(TravelTimeProvider):
    name = "google-routes"

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        fallback: TravelTimeProvider | None = None,
        timeout_s: float = 20.0,
        max_requests: int = 600,
        max_retries: int = 5,
        pace_s: float = 0.25,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
        if not self.api_key:
            raise RuntimeError("GOOGLE_MAPS_API_KEY is not set")
        self.cache_dir = Path(cache_dir or os.environ.get("NOTANA_CACHE_DIR", "data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fallback = fallback or SyntheticTravelTimeProvider()
        self.timeout_s = timeout_s
        self.max_requests = max_requests
        self.max_retries = max_retries
        self.pace_s = pace_s

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
        failed_blocks = 0
        ok_blocks = 0
        requests = 0
        errors: dict[str, int] = {}
        fatal: str | None = None
        departure = self._departure_time(departure_minute)
        for oi in range(0, n, BLOCK):
            for di in range(0, n, BLOCK):
                if fatal or requests >= self.max_requests:
                    failed_blocks += 1
                    continue
                origins = locations[oi : oi + BLOCK]
                dests = locations[di : di + BLOCK]
                try:
                    elements, tries = self._request_with_retry(origins, dests, departure)
                    requests += tries
                except _FatalRouteError as exc:
                    fatal = str(exc)
                    failed_blocks += 1
                    log.error("Routes API rejected the request: %s", exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - give up on this block only
                    failed_blocks += 1
                    key = type(exc).__name__ + (f" {getattr(exc, 'code', '')}" if hasattr(exc, "code") else "")
                    errors[key] = errors.get(key, 0) + 1
                    log.warning("Route matrix block failed after retries (%s); synthetic fallback", exc)
                    continue
                ok_blocks += 1
                for el in elements:
                    o = oi + el.get("originIndex", 0)
                    d = di + el.get("destinationIndex", 0)
                    if o == d or el.get("condition") != "ROUTE_EXISTS" or not el.get("duration"):
                        continue
                    # Add the same parking / door overhead the synthetic model uses.
                    minutes[o][d] = float(el["duration"].rstrip("s")) / 60.0 + self.fallback.config.per_trip_overhead_min  # type: ignore[attr-defined]
                    km[o][d] = el.get("distanceMeters", 0) / 1000.0
                    filled += 1
                time.sleep(self.pace_s)

        total = n * (n - 1)
        if ok_blocks == 0:
            source = "synthetic (google-routes unavailable)"
        elif failed_blocks:
            source = "google-routes+synthetic-fallback"
        else:
            source = "google-routes"
        notes = [f"{filled}/{total} elements from Routes API; {ok_blocks} blocks ok, {failed_blocks} failed ({requests} HTTP requests)"]
        if errors:
            notes.append("errors: " + ", ".join(f"{k} x{v}" for k, v in errors.items()))
        if fatal:
            notes.append(f"Routes API rejected the key/request: {fatal}")
        result = BaseMatrix(location_ids=[loc.id for loc in locations], minutes=minutes, km=km, source=source, notes=notes)
        if source == "google-routes":  # only complete matrices are cached
            path.write_text(json.dumps({"minutes": minutes, "km": km, "source": source, "notes": notes}))
        return result

    def _request_with_retry(self, origins: list[Location], dests: list[Location], departure: str) -> tuple[list[dict], int]:
        """Retry rate limits / transient errors with exponential backoff (honours Retry-After)."""
        delay = 2.0
        for attempt in range(1, self.max_retries + 2):
            try:
                return self._request(origins, dests, departure), attempt
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")[:300]
                if exc.code in (400, 401, 403, 404):
                    raise _FatalRouteError(f"HTTP {exc.code}: {body}") from exc
                if attempt > self.max_retries or exc.code not in (429, 500, 502, 503, 504):
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            except (urllib.error.URLError, TimeoutError):
                if attempt > self.max_retries:
                    raise
                wait = delay
            time.sleep(min(wait, 60.0))
            delay *= 2
        raise RuntimeError("unreachable")

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
            raise _FatalRouteError(data.get("error", {}).get("message", str(data)))
        return data
