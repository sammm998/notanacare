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


def test_google_is_opt_in_and_cached_data_is_reused_for_free(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from notana_planner import api
    from notana_planner.travel import google_routes as G

    monkeypatch.setenv("NOTANA_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    calls = []

    def no_network(self, origins, dests, departure, travel_mode="DRIVE"):
        calls.append(travel_mode)
        raise AssertionError("Google must not be called when live traffic is off")

    monkeypatch.setattr(G.GoogleRoutesTravelTimeProvider, "_request", no_network)
    client = TestClient(api.app)
    body = {"seed": 3, "target_interventions": 600, "employee_count": 10}
    sc = client.post("/api/scenarios", json=body).json()  # default: Google off
    assert calls == [] and sc["travel"]["source"] == "synthetic"
    assert sc["commute"]["source"] == "estimate"

    # A Google matrix fetched earlier for the same addresses is reused without calls.
    s = api.STORE.get(sc["id"])
    locs = list(s.world.scenario.locations.values())
    n = len(locs)
    path = tmp_path / f"google_matrix_{G.matrix_cache_key(locs, 8 * 60)}.json"
    path.write_text(json.dumps({"minutes": [[0 if i == j else 7 for j in range(n)] for i in range(n)],
                                "km": [[0.0] * n for _ in range(n)], "source": "google-routes", "notes": []}))
    again = client.post("/api/scenarios", json=body).json()
    assert calls == [] and again["travel"]["source"] == "google-routes"
