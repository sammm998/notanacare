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
        pace_s: float = 0.05,
        budget_s: float | None = None,
        workers: int = 3,
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
        # Wall-clock budget for one matrix: Routes API allows ~3,000 elements/minute,
        # a 165-address day is ~27,000, so the closest pairs are fetched first.
        self.budget_s = float(budget_s if budget_s is not None else os.environ.get("NOTANA_GOOGLE_BUDGET_S", 60))
        self.workers = workers
        self.progress = None  # optional callable(str) for job progress

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
        departure = self._departure_time(departure_minute)
        # Blocks of 25 locations, grouped by zone, requested closest pairs first:
        # routes almost only use short trips, so within a time budget the pairs
        # that matter come from Google and far pairs keep the synthetic estimate.
        order = sorted(range(n), key=lambda k: (locations[k].zone, locations[k].id))
        groups = [order[k:k + BLOCK] for k in range(0, n, BLOCK)]
        cent = [(sum(locations[k].lat for k in g) / len(g), sum(locations[k].lon for k in g) / len(g)) for g in groups]

        def dist(a: int, b: int) -> float:
            (la, lo), (lb, lb2) = cent[a], cent[b]
            return (la - lb) ** 2 + ((lo - lb2) * 0.55) ** 2

        pairs = sorted(((a, b) for a in range(len(groups)) for b in range(len(groups))), key=lambda p: (dist(*p), p))
        deadline = time.monotonic() + self.budget_s
        filled = ok_blocks = failed_blocks = skipped = requests = 0
        errors: dict[str, int] = {}
        fatal: str | None = None

        def fetch(a: int, b: int):  # noqa: ANN202
            return self._request_with_retry([locations[k] for k in groups[a]], [locations[k] for k in groups[b]],
                                            departure, deadline)

        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        pending = list(pairs)
        running: dict = {}
        with ThreadPoolExecutor(max(1, self.workers)) as ex:
            while pending or running:
                while pending and len(running) < self.workers and not fatal and time.monotonic() < deadline \
                        and requests + len(running) < self.max_requests:
                    a, b = pending.pop(0)
                    running[ex.submit(fetch, a, b)] = (a, b)
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    a, b = running.pop(fut)
                    try:
                        elements, tries = fut.result()
                        requests += tries
                    except _FatalRouteError as exc:
                        fatal = str(exc)
                        failed_blocks += 1
                        log.error("Routes API rejected the request: %s", exc)
                        continue
                    except Exception as exc:  # noqa: BLE001 - this block keeps the synthetic estimate
                        failed_blocks += 1
                        key = type(exc).__name__ + (f" {getattr(exc, 'code', '')}" if hasattr(exc, "code") else "")
                        errors[key] = errors.get(key, 0) + 1
                        continue
                    ok_blocks += 1
                    for el in elements:
                        o = groups[a][el.get("originIndex", 0)]
                        d = groups[b][el.get("destinationIndex", 0)]
                        if o == d or el.get("condition") != "ROUTE_EXISTS" or not el.get("duration"):
                            continue
                        # Add the same parking / door overhead the synthetic model uses.
                        minutes[o][d] = float(el["duration"].rstrip("s")) / 60.0 + self.fallback.config.per_trip_overhead_min  # type: ignore[attr-defined]
                        km[o][d] = el.get("distanceMeters", 0) / 1000.0
                        filled += 1
                    if self.progress:
                        self.progress(f"Google Routes: {ok_blocks + failed_blocks}/{len(pairs)} blocks "
                                      f"({filled} trips)")
                if self.pace_s:
                    time.sleep(self.pace_s)
            skipped = len(pending)

        total = n * (n - 1)
        if ok_blocks == 0:
            source = "synthetic (google-routes unavailable)"
        elif failed_blocks or skipped:
            source = "google-routes+synthetic-fallback"
        else:
            source = "google-routes"
        notes = [f"{filled}/{total} trips from Routes API, closest pairs first; {ok_blocks} blocks ok, "
                 f"{failed_blocks} failed, {skipped} over the {int(self.budget_s)} s budget use the synthetic estimate "
                 f"({requests} HTTP requests)"]
        if errors:
            notes.append("errors: " + ", ".join(f"{k} x{v}" for k, v in errors.items()))
        if fatal:
            notes.append(f"Routes API rejected the key/request: {fatal}")
        result = BaseMatrix(location_ids=[loc.id for loc in locations], minutes=minutes, km=km, source=source, notes=notes)
        if ok_blocks and not fatal:  # partial matrices are cached too (labelled), so a seed is fetched once
            path.write_text(json.dumps({"minutes": minutes, "km": km, "source": source, "notes": notes}))
        return result

    def _request_with_retry(self, origins: list[Location], dests: list[Location], departure: str,
                            deadline: float | None = None, travel_mode: str = "DRIVE") -> tuple[list[dict], int]:
        """Retry rate limits / transient errors with exponential backoff (honours Retry-After),
        but never past the time budget."""
        delay = 2.0
        for attempt in range(1, self.max_retries + 2):
            if deadline is not None and attempt > 1 and time.monotonic() > deadline:
                raise TimeoutError("time budget exhausted")
            try:
                return self._request(origins, dests, departure, travel_mode), attempt
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
            if deadline is not None and time.monotonic() + wait > deadline:
                raise TimeoutError("time budget exhausted while rate limited")
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

    def _request(self, origins: list[Location], dests: list[Location], departure: str,
                 travel_mode: str = "DRIVE") -> list[dict]:
        def wp(loc: Location) -> dict:
            return {"waypoint": {"location": {"latLng": {"latitude": loc.lat, "longitude": loc.lon}}}}

        body = {
            "origins": [wp(o) for o in origins],
            "destinations": [wp(d) for d in dests],
            "travelMode": travel_mode,
            "departureTime": departure,
        }
        if travel_mode == "DRIVE":
            body["routingPreference"] = "TRAFFIC_AWARE"
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
