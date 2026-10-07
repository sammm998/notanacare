"""Staff register (fixed homes) and cached commute times."""

import json

from notana_planner import staff_homes
from notana_planner.domain import ScenarioConfig
from notana_planner.generator import generate_scenario
from notana_planner.travel.google_routes import GoogleRoutesTravelTimeProvider


def _fake_google(tmp_path, calls):
    prov = GoogleRoutesTravelTimeProvider(api_key="test", cache_dir=tmp_path / "g")

    def fake_request(origins, dests, departure, travel_mode="DRIVE"):
        calls.append((len(origins), travel_mode))
        return [{"originIndex": i, "destinationIndex": 0, "duration": f"{600 + 60 * i}s",
                 "distanceMeters": 5000 + i, "condition": "ROUTE_EXISTS"} for i in range(len(origins))]

    prov._request = fake_request
    return prov


def test_homes_are_fixed_per_employee_not_per_scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTANA_CACHE_DIR", str(tmp_path))
    a = generate_scenario(ScenarioConfig(seed=1, target_interventions=600, employee_count=12))
    b = generate_scenario(ScenarioConfig(seed=2, target_interventions=600, employee_count=12))
    staff_homes.attach(list(a.employees.values()), a.locations, "stockholm")
    staff_homes.attach(list(b.employees.values()), b.locations, "stockholm")
    for eid in a.employees:
        assert a.employees[eid].home_address == b.employees[eid].home_address
        assert a.employees[eid].commute_minutes and a.employees[eid].commute_source == "estimate"


def test_register_edits_are_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTANA_CACHE_DIR", str(tmp_path))
    sc = generate_scenario(ScenarioConfig(seed=1, target_interventions=600, employee_count=12))
    staff_homes.homes_for("stockholm", list(sc.employees))
    path = tmp_path / "staff_homes_stockholm.json"
    reg = json.loads(path.read_text())
    reg["E-001"] = {"address": "Sveavägen 1, Stockholm", "district": "Vasastan", "lat": 59.333, "lon": 18.063}
    path.write_text(json.dumps(reg))
    staff_homes.attach(list(sc.employees.values()), sc.locations, "stockholm")
    assert sc.employees["E-001"].home_address == "Sveavägen 1, Stockholm"


def test_google_commute_is_fetched_once_then_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTANA_CACHE_DIR", str(tmp_path))
    calls = []
    prov = _fake_google(tmp_path, calls)
    sc = generate_scenario(ScenarioConfig(seed=1, target_interventions=900, employee_count=20))
    emps = list(sc.employees.values())
    first = staff_homes.attach(emps, sc.locations, "stockholm", prov)
    assert calls and first["requested_from_google"] == len(emps)
    assert {m for _, m in calls} <= {"DRIVE", "TRANSIT"}
    assert all(e.commute_source == "google" for e in emps)
    n = len(calls)
    again = generate_scenario(ScenarioConfig(seed=9, target_interventions=900, employee_count=20))
    second = staff_homes.attach(list(again.employees.values()), again.locations, "stockholm", prov)
    # Same employees, same homes: only trips to a different team office are new.
    assert second["from_cache"] > 0
    assert len(calls) - n <= len([e for e in again.employees.values()
                                  if e.start_location_id != sc.employees[e.id].start_location_id
                                  or e.travel_mode != sc.employees[e.id].travel_mode])
    third = staff_homes.attach(list(again.employees.values()), again.locations, "stockholm", prov)
    assert third["requested_from_google"] == 0 and third["from_cache"] == len(again.employees)
