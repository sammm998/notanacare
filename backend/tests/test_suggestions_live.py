"""Suggestions for unplanned visits, approved exceptions, alarms, VAB and the live day."""

import copy

import pytest

from notana_planner.domain import ScenarioConfig
from notana_planner.generator import generate_scenario
from notana_planner.incidents import Incident, apply_incident
from notana_planner.live import run_live
from notana_planner.planner import World, plan_day
from notana_planner.replanning import replan_with_strategy
from notana_planner.suggestions import apply_option, relaxed_options, suggest_for_plan
from notana_planner.validator import validate_plan

# Understaffed on purpose so that there are unplanned visits to work on.
TIGHT = dict(seed=5, target_interventions=1200, employee_count=18)


@pytest.fixture(scope="module")
def tight():
    w = World.create(generate_scenario(ScenarioConfig(**TIGHT)))
    return w, plan_day(w, time_limit_s=6)


def test_intervention_kpis_add_up(small_plan, small_world):
    s = small_plan.score
    total = sum(len(v.intervention_ids) for v in small_world.scenario.visits.values())
    assert s["interventions_total"] == total
    assert s["interventions_planned"] + s["interventions_unplanned"] == total
    assert 0 < s["interventions_planned_share"] <= 1
    assert s["longest_leg_minutes"] >= s["p90_leg_minutes"] >= s["median_leg_minutes"] >= 0


def test_every_unplanned_visit_gets_ranked_options(tight):
    w, plan = tight
    assert plan.unplanned, "scenario should be understaffed"
    sug = suggest_for_plan(w, plan, list(plan.unplanned)[:4], replan_time_s=2)
    assert sug
    for vid, opts in sug.items():
        assert opts, vid
        sev = [o["severity"] for o in opts]
        assert sev == sorted(sev), "least severe first"
        for o in opts:
            if o["kind"] == "replan":
                assert o["severity"] == 0 and o["_plan"].validation["valid"]
                assert vid in o["_plan"].assignments


def test_applied_exception_is_reported_not_hidden(tight):
    w, plan = tight
    wc = copy.deepcopy(w)
    vid = next(v for v in sorted(plan.unplanned) if wc.scenario.visits[v].required_employee_count == 1)
    opt = next(o for o in relaxed_options(wc, plan, wc.scenario.visits[vid]) if o["severity"] > 0)
    new = apply_option(wc, plan, opt)
    v = new.validation
    assert vid in new.assignments and vid not in new.unplanned
    assert v["valid"] and v["status"] == "VALID_WITH_APPROVED_EXCEPTIONS"
    assert v["approved_exceptions"], "the validator must still report the deviations"
    # Without the approval the very same plan is INVALID.
    bare = copy.deepcopy(new)
    bare.exceptions = []
    rep = validate_plan(wc.scenario, wc.travel(), bare, max_overtime=wc.settings.max_overtime_min)
    assert not rep.valid


def test_alarm_dispatches_fastest_responder_and_stays_valid(small_world, small_plan):
    w = copy.deepcopy(small_world)
    plan = small_plan
    clock = 10 * 60 + 40
    sc = w.scenario
    busy = {sc.visits[v].recipient_id for v, a in plan.assignments.items() if a.start - 40 < clock < a.end + 90}
    rid = next(r for r in sorted(sc.recipients) if r not in busy)
    eff = apply_incident(w, plan, Incident("alarm", clock, {"recipient_id": rid}))
    res, _ = replan_with_strategy(w, plan, eff)
    vid = eff.facts["alarm_visit"]
    assert res.plan.validation["valid"]
    a = res.plan.assignments[vid]
    assert a.employee_ids == [eff.facts["dispatched"]] and a.start == eff.facts["arrive"] >= clock


def test_alarm_at_recipient_with_visit_in_progress_is_rejected(small_world, small_plan):
    w = copy.deepcopy(small_world)
    vid, a = next(iter(sorted(small_plan.assignments.items(), key=lambda x: x[1].start)))
    rid = w.scenario.visits[vid].recipient_id
    with pytest.raises(ValueError, match="in progress"):
        apply_incident(w, small_plan, Incident("alarm", a.start + 1, {"recipient_id": rid}))


def test_child_sick_employee_leaves_and_plan_stays_valid(small_world, small_plan):
    w = copy.deepcopy(small_world)
    clock = 11 * 60
    eid = next(e for e, r in sorted(small_plan.routes.items()) if sum(1 for s in r.stops if s.kind == "visit" and s.start > clock) >= 2)
    eff = apply_incident(w, small_plan, Incident("child_sick", clock, {"employee_id": eid}))
    res, _ = replan_with_strategy(w, small_plan, eff)
    assert res.plan.validation["valid"]
    leaves = eff.facts["leaves_at"]
    assert all(s.start < leaves for s in res.plan.routes[eid].stops if s.kind == "visit")


def test_live_day_every_plan_valid(small_world, small_plan):
    w = copy.deepcopy(small_world)
    cur, feed = run_live(w, small_plan, events=5, seed=3, pace_s=0)
    real = [e for e in feed if not e.get("skipped")]
    assert real
    assert all(e["valid"] for e in real), [(e["label"], e["status"]) for e in real]
    for e in real:
        if e["kind"] == "alarm":
            assert e["response_minutes"] is not None and e["response_minutes"] >= 0
