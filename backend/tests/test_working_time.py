"""Arbetstidslagen: shift templates, solver bounds and validator checks."""

import copy

from notana_planner.domain import BreakRule, Employee, Interval, WorkingTimeRules
from notana_planner.generator import SHIFT_TEMPLATES
from notana_planner.planner import World, plan_day
from notana_planner.validator import validate_plan
from notana_planner.working_time import clamp_break, latest_end

RULES = WorkingTimeRules()


def _emp(start, end, brk=None, gap=None):
    return Employee(
        id="E-T", name="T", team="X", shift_start=start, shift_end=end, start_location_id="B",
        end_location_id="B", skills=[], delegations=[],
        breaks=[BreakRule(30, *brk)] if brk else [],
        unavailable=[Interval(gap[0], gap[1], "split-shift gap")] if gap else [],
    )


def test_shift_templates_comply_with_atl():
    for name, s, e, gap, brk, *_ in SHIFT_TEMPLATES:
        emp = _emp(s, e, brk, gap)
        assert latest_end(emp, RULES, 0) == e, name  # no cut needed without overtime
        rests = ([(gap[0], gap[1])] if gap else []) + ([(brk[1], brk[1] + 30)] if brk else [])
        worked = (e - s) - sum(b - a for a, b in rests)
        assert worked <= RULES.max_daily_work_min, name
        assert e - s <= 24 * 60 - RULES.min_daily_rest_min, name
        # Latest-possible break still leaves <= 5 h on both sides.
        prev = s
        for a, b in sorted(rests) + [(e, e)]:
            assert a - prev <= RULES.max_continuous_work_min, (name, a - prev)
            prev = b


def test_overtime_is_capped_by_continuous_work_rule():
    emp = _emp(13 * 60 + 30, 22 * 60 + 15, (16 * 60 + 45, 17 * 60 + 45))
    end = latest_end(emp, RULES, 120)
    assert end == 17 * 60 + 45 + 30 + 300  # 23:15, not 00:15
    b = clamp_break(emp.breaks[0], emp, RULES, end)
    assert b.earliest_start == end - 30 - 300 and b.latest_start >= b.earliest_start


def test_no_break_part_time_cannot_exceed_five_hours():
    emp = _emp(17 * 60 + 15, 22 * 60 + 15)
    assert latest_end(emp, RULES, 90) == 17 * 60 + 15 + 300


def test_validator_flags_continuous_work(small_world, plan_copy):
    eid, r = next((e, r) for e, r in plan_copy.routes.items()
                  if any(s.kind == "break" for s in r.stops) and r.route_end is not None)
    r.stops = [s for s in r.stops if s.kind != "break"]
    sc = copy.deepcopy(small_world.scenario)
    sc.employees[eid].breaks = []  # isolate the ATL rule from BREAK_MISSING
    r.route_start = sc.employees[eid].shift_start
    r.route_end = max(r.route_end, r.route_start + 301)
    sc.employees[eid].shift_end = max(sc.employees[eid].shift_end, r.route_end)
    rep = validate_plan(sc, small_world.travel(), plan_copy)
    assert "ATL_CONTINUOUS_WORK" in {e.code for e in rep.errors}


def test_validator_flags_dygnsvila_towards_tomorrow(small_world, small_plan):
    sc = copy.deepcopy(small_world.scenario)
    eid, r = next((e, r) for e, r in small_plan.routes.items() if r.route_end is not None)
    sc.employees[eid].latest_end_by_rest = r.route_end - 1
    rep = validate_plan(sc, small_world.travel(), small_plan)
    assert "DAILY_REST_VIOLATED" in {e.code for e in rep.errors}


def test_optimizer_with_overtime_stays_atl_compliant(small_world):
    w = World(small_world.scenario, small_world.base_matrix, weights=small_world.weights,
              settings=copy.deepcopy(small_world.settings))
    w.settings.max_overtime_min = 120
    plan = plan_day(w, time_limit_s=6)
    rep = validate_plan(w.scenario, w.travel(), plan, max_overtime=120)
    assert rep.valid, rep.errors[:3]
    assert rep.checks.get("working_time", 0) > 0
