import time
from types import SimpleNamespace

import anthropic
import httpx
from fastapi.testclient import TestClient

from notana_planner.advisor.base import clamp_params
from notana_planner.advisor.claude import ClaudePlanningAdvisor
from notana_planner.advisor.deterministic import DeterministicAdvisor
from notana_planner.classifier import Alternative, MockDecisionClassifier


def test_clamp_params_enforces_bounds():
    p = clamp_params({"start_phase": 9, "phase1_helpers": -3, "phase3_time_s": 1000, "junk": 1, "enhanced_repair": 1})
    assert p == {"start_phase": 3, "phase1_helpers": 2, "phase3_time_s": 45.0, "enhanced_repair": True}


def test_deterministic_advisor():
    d = DeterministicAdvisor().suggest_repair_strategy({"incident_kind": "employee_sick", "released_visits": 20, "released_high_priority": 2})
    assert d.params["phase1_helpers"] == 12 and d.params["enhanced_repair"] is True
    assert d.source == "deterministic"


class _FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if self.behaviour == "error":
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200)
        if self.behaviour == "refusal":
            return SimpleNamespace(stop_reason="refusal", content=[], usage=usage)
        text = '{"start_phase": 2, "phase1_helpers": 99, "phase1_horizon_min": 120, "phase2_helpers": 30, "enhanced_repair": false, "rationale": "wide"}'
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)], usage=usage)


def _advisor(behaviour):
    msgs = _FakeMessages(behaviour)
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    return ClaudePlanningAdvisor(model="claude-opus-5-5", client=client), msgs


def test_claude_advisor_parses_clamps_and_costs():
    adv, msgs = _advisor("ok")
    d = adv.suggest_repair_strategy({"incident_kind": "traffic"})
    assert d.source == "claude:claude-opus-5-5"
    assert d.params["phase1_helpers"] == 20  # clamped
    assert d.usage["cost_usd"] == round(1000 / 1e6 * 4 + 200 / 1e6 * 20, 5)
    assert msgs.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_claude_advisor_falls_back_on_error_and_refusal():
    for b in ("error", "refusal"):
        adv, _ = _advisor(b)
        d = adv.suggest_repair_strategy({"incident_kind": "employee_sick", "released_visits": 2})
        assert d.source.startswith("deterministic (fallback")


def test_classifier_never_ranks_invalid():
    c = MockDecisionClassifier()
    ranked = c.rank_alternatives([
        Alternative("1", True, {"unplanned": 3}),
        Alternative("2", False, {"unplanned": 0}),
        Alternative("3", True, {"unplanned": 1}),
    ])
    assert [k for k, _ in ranked] == ["3", "1"]


def _wait(client, jid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.5)
    raise AssertionError("job timeout")


def test_api_end_to_end(tmp_path, monkeypatch):
    from notana_planner import api

    client = TestClient(api.app)
    assert client.get("/api/health").json()["ok"]
    sc = client.post("/api/scenarios", json={"seed": 4, "target_interventions": 700, "employee_count": 16}).json()
    assert sc["counts"]["visits"] > 50
    job = client.post(f"/api/scenarios/{sc['id']}/plan", json={"time_limit_s": 4}).json()
    j = _wait(client, job["job_id"])
    assert j["status"] == "done", j.get("error")
    pid = j["result"]["plan_id"]
    plan = client.get(f"/api/plans/{pid}").json()
    assert plan["validation"]["valid"]
    vid = next(iter(plan["assignments"]))
    detail = client.get(f"/api/plans/{pid}/visits/{vid}").json()
    assert detail["assignment"]["staff"][0]["why"]
    inc = client.post(f"/api/scenarios/{sc['id']}/incidents",
                      json={"kind": "employee_sick", "clock": 600, "params": {"random": 1, "seed": 3}}).json()
    j2 = _wait(client, inc["job_id"])
    assert j2["status"] == "done", j2.get("error")
    assert "Independent validation" in j2["result"]["summary"]
    assert client.post(f"/api/plans/{j2['result']['after_plan_id']}/validate").json()["valid"]
    bad = client.post(f"/api/scenarios/{sc['id']}/incidents", json={"kind": "meteor", "clock": 600})
    assert bad.status_code == 400


def test_visit_recommendation_can_be_accepted():
    from notana_planner import api

    client = TestClient(api.app)
    sc = client.post("/api/scenarios", json={"seed": 5, "target_interventions": 900, "employee_count": 12}).json()
    j = _wait(client, client.post(f"/api/scenarios/{sc['id']}/plan", json={"time_limit_s": 4}).json()["job_id"])
    pid = j["result"]["plan_id"]
    plan = client.get(f"/api/plans/{pid}").json()
    assert plan["unplanned"], "scenario should be understaffed"
    vid = next(iter(plan["unplanned"]))
    rec = client.post(f"/api/plans/{pid}/visits/{vid}/suggestions").json()
    assert rec["visit_id"] == vid and rec["options"]
    sev = [o["severity"] for o in rec["options"]]
    assert sev == sorted(sev)
    opt = next(o for o in rec["options"] if o["kind"] != "pool" and o["level"] != "not_permitted")
    r = client.post(f"/api/plans/{pid}/suggestions/{opt['id']}/apply").json()
    after = client.get(f"/api/plans/{r['plan_id']}").json()
    assert vid in after["assignments"] and after["validation"]["valid"]
    planned_vid = next(iter(plan["assignments"]))
    assert client.post(f"/api/plans/{pid}/visits/{planned_vid}/suggestions").status_code in (400, 409)


def test_closed_window_points_to_earlier_plan_where_it_is_solvable():
    from notana_planner import api

    client = TestClient(api.app)
    sc = client.post("/api/scenarios", json={"seed": 5, "target_interventions": 900, "employee_count": 12}).json()
    j = _wait(client, client.post(f"/api/scenarios/{sc['id']}/plan", json={"time_limit_s": 4}).json()["job_id"])
    pid = j["result"]["plan_id"]
    plan = client.get(f"/api/plans/{pid}").json()
    inc = client.post(f"/api/scenarios/{sc['id']}/incidents",
                      json={"kind": "employee_delayed", "clock": 19 * 60,
                            "params": {"employee_id": next(iter(plan["routes"])), "minutes": 10}}).json()
    j2 = _wait(client, inc["job_id"])
    assert j2["status"] == "done", j2.get("error")
    late = j2["result"]["after_plan_id"]
    after = client.get(f"/api/plans/{late}").json()
    early = [v for v in after["unplanned"] if v in plan["unplanned"]
             and next(x for x in sc_visits(client, sc["id"]) if x["id"] == v)["latest"] < 19 * 60]
    assert early, "need an unplanned visit whose window closed before 19:00"
    rec = client.post(f"/api/plans/{late}/visits/{early[0]}/suggestions").json()
    assert rec["options"] == [] and rec["window_closed"]
    assert rec["earlier"]["plan_id"] == pid and rec["earlier"]["options"]
    opt = next(o for o in rec["earlier"]["options"] if o["kind"] != "pool" and o["level"] != "not_permitted")
    client.post(f"/api/plans/{pid}/activate")
    r = client.post(f"/api/plans/{pid}/suggestions/{opt['id']}/apply").json()
    assert early[0] in client.get(f"/api/plans/{r['plan_id']}").json()["assignments"]


def sc_visits(client, sid):
    return client.get(f"/api/scenarios/{sid}").json()["visits"]
