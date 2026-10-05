"""End-to-end planning and re-planning on a small scenario."""

from notana_planner.domain import EmployeeStatus
from notana_planner.incidents import Incident, apply_incident
from notana_planner.replanning import clone_world, replan, replan_with_strategy


def test_plan_basics(small_world, small_plan):
    sc = small_world.scenario
    assert small_plan.validation["valid"]
    assert small_plan.score["planned_visits"] + small_plan.score["unplanned_visits"] == len(sc.visits)
    assert small_plan.score["planned_visits"] > 0.75 * len(sc.visits)
    # Every unplanned visit carries at least one structured reason.
    assert all(u.reasons for u in small_plan.unplanned.values())
    # Double staffing: two different employees, identical start.
    for vid, a in small_plan.assignments.items():
        v = sc.visits[vid]
        assert len(a.employee_ids) == v.required_employee_count
        assert len(set(a.starts.values())) == 1


def _busy_employee(plan, clock):
    return max(plan.routes, key=lambda e: (sum(1 for s in plan.routes[e].stops if s.kind == "visit" and s.start > clock), e))


def test_sick_employee_replan(small_world, small_plan):
    w = clone_world(small_world)
    clock = 10 * 60
    eid = _busy_employee(small_plan, clock)
    eff = apply_incident(w, small_plan, Incident("employee_sick", clock, {"employee_ids": [eid]}))
    assert eff.released_visits
    res = replan(w, small_plan, eff)
    new = res.plan
    assert new.validation["valid"], new.validation["errors"][:3]
    # No non-started work remains with the sick employee.
    assert all(s.locked or s.start <= clock for s in new.routes[eid].stops)
    # Started visits are unchanged.
    for vid, a in small_plan.assignments.items():
        if a.start <= clock:
            assert new.assignments[vid].start == a.start
            assert sorted(new.assignments[vid].employee_ids) == sorted(a.employee_ids)
    assert res.phases and res.summary.startswith(f"Employee {eid} reported sick")
    assert "Independent validation: VALID" in res.summary


def test_sick_before_shift_marks_status(small_world, small_plan):
    w = clone_world(small_world)
    eid = max(w.scenario.employees.values(), key=lambda e: e.shift_start).id
    clock = w.scenario.employees[eid].shift_start - 30
    apply_incident(w, small_plan, Incident("employee_sick", clock, {"employee_ids": [eid]}))
    assert w.scenario.employees[eid].status == EmployeeStatus.SICK


def test_medication_moved_respects_new_window(small_world, small_plan):
    w = clone_world(small_world)
    clock = 9 * 60
    sc = w.scenario
    vid = next(vid for vid, a in sorted(small_plan.assignments.items(), key=lambda x: x[1].start)
               if a.start > 12 * 60 and any(sc.interventions[i].type == "medication" for i in sc.visits[vid].intervention_ids))
    eff = apply_incident(w, small_plan, Incident("medication_moved", clock, {"visit_id": vid, "new_time": 11 * 60, "window_minutes": 15}))
    res = replan(w, small_plan, eff)
    assert res.plan.validation["valid"], res.plan.validation["errors"][:3]
    # Whatever visit now holds the moved medication must start within 10:45-11:15 (or be reported unplanned).
    iid = eff.facts["intervention"]
    holder = next(v for v in sc.visits.values() if iid in v.intervention_ids)
    a = res.plan.assignments.get(holder.id)
    if a is not None:
        assert 10 * 60 + 45 <= a.start <= 11 * 60 + 15
    else:
        assert holder.id in res.plan.unplanned


def test_traffic_and_delay_replans_stay_valid(small_world, small_plan):
    w = clone_world(small_world)
    clock = 10 * 60
    zone = w.scenario.locations[w.scenario.bases[0]].zone
    eff = apply_incident(w, small_plan, Incident("traffic", clock, {"zone": zone, "multiplier": 2.0}))
    res = replan(w, small_plan, eff)
    assert res.plan.validation["valid"], res.plan.validation["errors"][:3]
    eid = _busy_employee(res.plan, clock + 10)
    eff2 = apply_incident(w, res.plan, Incident("employee_delayed", clock + 10, {"employee_id": eid, "minutes": 30}))
    res2 = replan(w, res.plan, eff2)
    assert res2.plan.validation["valid"], res2.plan.validation["errors"][:3]
    for s in res2.plan.routes[eid].stops:
        if not s.locked and s.start > clock + 10:
            assert s.start >= w.scenario.employees[eid].available_from


def test_user_locked_visit_is_kept(small_world, small_plan):
    w = clone_world(small_world)
    clock = 9 * 60
    vid, a = next((v, a) for v, a in sorted(small_plan.assignments.items(), key=lambda x: x[1].start) if a.start > 13 * 60)
    w.scenario.visits[vid].locked = True
    other = next(e for e in small_plan.routes if e not in a.employee_ids and small_plan.routes[e].stops)
    eff = apply_incident(w, small_plan, Incident("employee_sick", clock, {"employee_ids": [other]}))
    res = replan(w, small_plan, eff)
    assert res.plan.validation["valid"]
    assert res.plan.assignments[vid].start == a.start
    assert sorted(res.plan.assignments[vid].employee_ids) == sorted(a.employee_ids)


def test_strategies_run_and_report(small_world, small_plan):
    for strat in ("enhanced", "advisor", "classifier"):
        w = clone_world(small_world)
        eff = apply_incident(w, small_plan, Incident("employee_sick", 10 * 60, {"employee_ids": [_busy_employee(small_plan, 600)]}))
        res, meta = replan_with_strategy(w, small_plan, eff, strat)
        assert res.plan.validation["valid"]
        assert meta["strategy"] == strat
        if strat == "advisor":
            assert meta["advisor"]["source"].startswith(("deterministic", "claude"))
        if strat == "classifier":
            assert meta["classification"]["urgency"] in ("low", "medium", "high")


def test_incident_chain_regression_stays_valid():
    """Sick -> traffic -> delay -> medication moved (the experiment sequence) on the
    scenario that exposed break / split-shift / half-staffed-double bugs: every
    repair phase must validate."""
    from notana_planner.domain import ScenarioConfig
    from notana_planner.experiments import default_incidents
    from notana_planner.generator import generate_scenario
    from notana_planner.planner import World, plan_day

    sc = generate_scenario(ScenarioConfig(seed=42, target_interventions=1200, employee_count=26))
    w0 = World.create(sc)
    base = plan_day(w0, "baseline", 6)
    w = clone_world(w0)
    plan = base
    for inc in default_incidents(base, sc):
        eff = apply_incident(w, plan, inc)
        res = replan(w, plan, eff)
        for ph in res.phases:
            assert ph.plan.validation["valid"], (inc.kind, ph.phase, ph.plan.validation["errors"][:3])
        plan = res.plan
