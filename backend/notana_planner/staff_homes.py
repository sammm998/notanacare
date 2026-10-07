"""Where the staff live and how long it takes them to get to work.

Home addresses come from a **staff register**, one JSON file per area in the
cache directory (``NOTANA_CACHE_DIR``, default ``data/cache``):
``staff_homes_<area>.json`` = ``{employee id: {address, district, lat, lon}}``.
An employee keeps the same home in every scenario and on every restart: a
missing employee is given a plausible home once (seeded by area and employee
id only, not by the scenario seed) and written to the register. Real
addresses can be entered by editing the file; entries in it are never changed.

The commute is the trip from home to the employee's team office (where the
shift starts), by car for drivers and by public transport for the others. It
is the employee's own time, shown for information; it is not part of the
route. Each (home, office, mode) trip is computed once and stored in
``commute_cache.json``: Google Routes when a key is configured, otherwise a
labelled estimate. A new scenario only asks Google for trips that are not in
the cache yet, so the API is not called again for the same addresses.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable

from .domain import Employee, Location
from .travel.synthetic import SyntheticTravelTimeProvider

log = logging.getLogger(__name__)
_LOCK = threading.Lock()

# Residential districts staff live in: (name, lat, lon, radius km, weight, streets).
# Inner-city districts weigh less (housing costs), suburbs more.
DISTRICTS: dict[str, tuple[tuple, ...]] = {
    "stockholm": (
        ("Södermalm", 59.3150, 18.0710, 1.0, 0.6, ("Götgatan", "Ringvägen", "Hornsgatan", "Folkungagatan")),
        ("Vasastan", 59.3445, 18.0450, 0.8, 0.4, ("Odengatan", "Upplandsgatan", "Dalagatan")),
        ("Kungsholmen", 59.3320, 18.0300, 0.7, 0.4, ("Fleminggatan", "Hantverkargatan", "Sankt Eriksgatan")),
        ("Östermalm", 59.3390, 18.0880, 0.8, 0.3, ("Karlavägen", "Valhallavägen", "Sandhamnsgatan")),
        ("Bromma", 59.3380, 17.9400, 1.4, 0.7, ("Bromma kyrkväg", "Drottningholmsvägen", "Ulvsundavägen")),
        ("Hägersten", 59.3010, 17.9850, 1.2, 0.9, ("Hägerstensvägen", "Västertorpsvägen", "Personnevägen")),
        ("Farsta", 59.2440, 18.0900, 1.2, 1.0, ("Farstagången", "Lingvägen", "Larsbodavägen")),
        ("Solna", 59.3600, 18.0010, 1.0, 0.8, ("Solnavägen", "Råsundavägen", "Frösundaleden")),
        ("Sundbyberg", 59.3610, 17.9710, 0.9, 0.8, ("Landsvägen", "Sturegatan", "Järnvägsgatan")),
        ("Kista", 59.4030, 17.9440, 1.1, 0.8, ("Kistagången", "Isafjordsgatan", "Edvard Griegs gång")),
        ("Rinkeby", 59.3880, 17.9280, 0.9, 0.9, ("Rinkebystråket", "Kuddbygränd", "Skårbygränd")),
        ("Skärholmen", 59.2770, 17.9070, 1.0, 1.0, ("Skärholmsvägen", "Bredholmsgatan", "Vårbyvägen")),
        ("Älvsjö", 59.2790, 18.0110, 1.0, 0.8, ("Götalandsvägen", "Älvsjö gårdsväg", "Svartlösavägen")),
        ("Huddinge", 59.2370, 17.9810, 1.6, 1.0, ("Sjödalsvägen", "Storängsvägen", "Kommunalvägen")),
        ("Haninge", 59.1690, 18.1440, 1.8, 0.8, ("Handens stationsväg", "Dalarövägen", "Nynäsvägen")),
        ("Nacka", 59.3100, 18.1630, 1.4, 0.7, ("Värmdövägen", "Vikdalsvägen", "Järlaleden")),
        ("Järfälla", 59.4280, 17.8360, 1.6, 0.7, ("Jakobsbergsgatan", "Mälarvägen", "Viksjöleden")),
        ("Hässelby", 59.3670, 17.8330, 1.2, 0.6, ("Maltesholmsvägen", "Hässelby torg", "Lyckselevägen")),
    ),
    "goteborg": (
        ("Majorna", 57.6940, 11.9200, 0.9, 0.6, ("Karl Johansgatan", "Mariaplan", "Kungsladugårdsgatan")),
        ("Centrum", 57.7060, 11.9700, 0.8, 0.3, ("Linnégatan", "Vasagatan", "Aschebergsgatan")),
        ("Hisingen", 57.7300, 11.9400, 1.5, 1.0, ("Hisingsgatan", "Vågmästaregatan", "Wieselgrensgatan")),
        ("Örgryte", 57.7000, 12.0100, 1.0, 0.5, ("Sankt Pauligatan", "Kungsportsavenyen", "Danska vägen")),
        ("Frölunda", 57.6550, 11.9100, 1.2, 0.9, ("Frölundagatan", "Näverlursgatan", "Opaltorget")),
        ("Angered", 57.7980, 12.0500, 1.3, 1.0, ("Angereds torg", "Rannebergsvägen", "Hjällbovägen")),
        ("Kortedala", 57.7480, 12.0350, 1.0, 0.8, ("Kortedala torg", "Aprilgatan", "Julaftonsgatan")),
        ("Bergsjön", 57.7560, 12.0740, 1.0, 0.8, ("Rymdtorget", "Kosmosgatan", "Galaxgatan")),
        ("Mölndal", 57.6550, 12.0140, 1.3, 0.8, ("Kvarnbygatan", "Göteborgsvägen", "Frölundagatan")),
        ("Partille", 57.7400, 12.1060, 1.3, 0.6, ("Gamla Kronvägen", "Partilledalsvägen", "Ugglumsleden")),
    ),
    "uppsala": (
        ("Centrum", 59.8586, 17.6389, 0.8, 0.4, ("Svartbäcksgatan", "Kungsgatan", "Sysslomansgatan")),
        ("Luthagen", 59.8650, 17.6200, 0.7, 0.5, ("Luthagsesplanaden", "Väderkvarnsgatan", "Geijersgatan")),
        ("Gottsunda", 59.8100, 17.6200, 1.0, 1.0, ("Gottsundavägen", "Valthornsvägen", "Hugo Alfvéns väg")),
        ("Sävja", 59.8200, 17.7000, 1.0, 0.8, ("Sävja Allé", "Bärbyleden", "Tvärvägen")),
        ("Gränby", 59.8800, 17.6800, 1.0, 0.8, ("Gränbyvägen", "Rosendalsvägen", "Salabacksgatan")),
        ("Eriksberg", 59.8530, 17.5960, 0.9, 0.7, ("Eriksbergsvägen", "Norbyvägen", "Granitvägen")),
        ("Stenhagen", 59.8450, 17.5600, 0.9, 0.8, ("Stenhagens centrum", "Hamnesplanaden", "Fältvägen")),
        ("Storvreta", 59.9600, 17.7050, 1.0, 0.5, ("Centralvägen", "Ringvägen", "Fyrislundsvägen")),
        ("Knivsta", 59.7250, 17.7880, 1.0, 0.5, ("Centralvägen", "Thunmans väg", "Ängbyvägen")),
    ),
}

# Estimates (no Google key): door-to-door averages for commutes in a Swedish city.
CAR_COMMUTE_KMH = 36.0  # main roads in morning traffic
CAR_PARKING_MIN = 4
TRANSIT_KMH = 26.0  # metro / commuter train / bus incl. changes
TRANSIT_EXTRA_MIN = 8  # walking to the stop and waiting


def cache_dir() -> Path:
    d = Path(os.environ.get("NOTANA_CACHE_DIR", "data/cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(path)


def _new_home(area: str, eid: str) -> dict:
    rng = random.Random(f"home:{area}:{eid}")
    districts = DISTRICTS.get(area) or DISTRICTS["stockholm"]
    name, lat, lon, radius, _, streets = rng.choices(districts, weights=[d[4] for d in districts])[0]
    # Uniform point in a disc of the district's radius.
    r = radius * rng.random() ** 0.5
    a = rng.uniform(0, 2 * math.pi)
    dlat = r * math.cos(a) / 111.0
    dlon = r * math.sin(a) / (111.0 * math.cos(math.radians(lat)))
    return {
        "address": f"{rng.choice(streets)} {rng.randint(1, 120)}, {name}",
        "district": name,
        "lat": round(lat + dlat, 6),
        "lon": round(lon + dlon, 6),
        "source": "generated",
    }


def homes_for(area: str, employee_ids: list[str]) -> dict[str, dict]:
    """The register entries for these employees; missing ones are created once and saved."""
    path = cache_dir() / f"staff_homes_{area}.json"
    with _LOCK:
        reg = _read(path)
        missing = [e for e in employee_ids if e not in reg]
        for eid in missing:
            reg[eid] = _new_home(area, eid)
        if missing:
            _write(path, reg)
    return {e: reg[e] for e in employee_ids}


def _key(mode: str, home: dict, office: Location, source: str) -> str:
    return f"{source}|{mode}|{home['lat']:.5f},{home['lon']:.5f}|{office.lat:.5f},{office.lon:.5f}"


def _estimate(home: dict, office: Location, mode: str) -> tuple[int, float]:
    """Commute estimate when Google is not configured. Commutes use main roads and
    the metro/commuter trains, so they are faster per km than trips between visits."""
    h = Location("home", home["lat"], home["lon"], "")
    _, km = SyntheticTravelTimeProvider().trip(h, office)
    if mode == "transit":
        minutes = km / TRANSIT_KMH * 60 + TRANSIT_EXTRA_MIN
    else:
        minutes = km / CAR_COMMUTE_KMH * 60 + CAR_PARKING_MIN
    return max(3, round(minutes)), round(km, 1)


def _google(provider, todo: list[tuple[str, dict, Location, str]], progress, budget_s: float = 45.0) -> dict:  # noqa: ANN001
    """{employee id: (minutes, km)} from Google Routes, one request per office and mode (<= 25 homes)."""
    from .travel.google_routes import GoogleRoutesTravelTimeProvider

    out: dict[str, tuple[int, float]] = {}
    groups: dict[tuple[str, str], list[tuple[str, dict, Location]]] = {}
    for eid, home, office, mode in todo:
        groups.setdefault((office.id, mode), []).append((eid, home, office))
    deadline = time.monotonic() + budget_s
    departure = GoogleRoutesTravelTimeProvider._departure_time(7 * 60)
    done = 0
    for (_, mode), items in groups.items():
        for i in range(0, len(items), 25):
            chunk = items[i:i + 25]
            if time.monotonic() > deadline:
                return out
            origins = [Location(eid, h["lat"], h["lon"], "") for eid, h, _ in chunk]
            try:
                elements, _ = provider._request_with_retry(
                    origins, [chunk[0][2]], departure, deadline=deadline,
                    travel_mode="TRANSIT" if mode == "transit" else "DRIVE")
            except Exception as exc:  # noqa: BLE001 - fall back to the estimate for this group
                log.warning("commute request failed: %s", exc)
                continue
            for el in elements:
                oi = el.get("originIndex", 0)
                dur = el.get("duration")
                if dur is None or el.get("condition") not in (None, "ROUTE_EXISTS"):
                    continue
                out[chunk[oi][0]] = (max(1, round(float(str(dur).rstrip("s")) / 60)),
                                     round(el.get("distanceMeters", 0) / 1000, 1))
            done += len(chunk)
            if progress:
                progress(f"commute times from Google: {done}/{len(todo)}")
    return out


def attach(employees: list[Employee], locations: dict[str, Location], area: str, provider=None,  # noqa: ANN001
           progress: Callable[[str], None] | None = None) -> dict:
    """Give every employee their registered home and commute time (cached). Returns a summary."""
    homes = homes_for(area, [e.id for e in employees])
    use_google = provider is not None and getattr(provider, "name", "") == "google-routes"
    source = "google" if use_google else "estimate"
    path = cache_dir() / "commute_cache.json"
    with _LOCK:
        cache = _read(path)
    todo, results = [], {}
    for e in employees:
        office = locations.get(e.start_location_id)
        if office is None:
            continue
        home = homes[e.id]
        mode = "car" if e.travel_mode == "car" else "transit"
        k = _key(mode, home, office, source)
        g = _key(mode, home, office, "google")  # fetched earlier: free to reuse even when Google is off
        if g in cache:
            results[e.id] = (cache[g]["minutes"], cache[g]["km"], "google")
        elif k in cache:
            results[e.id] = (cache[k]["minutes"], cache[k]["km"], source)
        else:
            todo.append((e.id, home, office, mode))
    fetched = 0
    if todo and use_google:
        got = _google(provider, todo, progress)
        fetched = len(got)
        for eid, home, office, mode in todo:
            if eid in got:
                cache[_key(mode, home, office, source)] = {"minutes": got[eid][0], "km": got[eid][1]}
                results[eid] = (*got[eid], "google")
    for eid, home, office, mode in todo:
        if eid not in results:
            m, km = _estimate(home, office, mode)
            if not use_google:
                cache[_key(mode, home, office, "estimate")] = {"minutes": m, "km": km}
            results[eid] = (m, km, "estimate")
    if todo:
        with _LOCK:
            merged = _read(path)
            merged.update(cache)
            _write(path, merged)
    for e in employees:
        h = homes[e.id]
        e.home_address, e.home_district = h["address"], h.get("district", "")
        e.home_lat, e.home_lon = h["lat"], h["lon"]
        if e.id in results:
            e.commute_minutes, e.commute_km, e.commute_source = results[e.id]
            e.commute_mode = "car" if e.travel_mode == "car" else "transit"
    mins = sorted(e.commute_minutes for e in employees if e.commute_minutes is not None)
    return {
        "employees": len(employees),
        "from_cache": len(employees) - len(todo),
        "requested_from_google": fetched,
        "source": source,
        "median_minutes": mins[len(mins) // 2] if mins else None,
        "register": str(cache_dir() / f"staff_homes_{area}.json"),
    }


def summary(employees: list[Employee]) -> dict | None:
    mins = sorted(e.commute_minutes for e in employees if e.commute_minutes is not None)
    if not mins:
        return None
    srcs = {e.commute_source for e in employees if e.commute_source}
    return {
        "median_minutes": mins[len(mins) // 2],
        "p90_minutes": mins[min(len(mins) - 1, int(0.9 * len(mins)))],
        "max_minutes": mins[-1],
        "over_45": sum(1 for m in mins if m > 45),
        "source": "google" if srcs == {"google"} else ("estimate" if srcs == {"estimate"} else "mixed"),
        "employees": len(mins),
    }

