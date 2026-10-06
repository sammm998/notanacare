"""Week planning: roster, cross-day rules and the week validator."""

import copy

import pytest

from notana_planner.config import ObjectiveWeights, SolverSettings
from notana_planner.domain import ScenarioConfig
from notana_planner.generator import WEEKLY_FREQUENCY, generate_week
from notana_planner.planner import World
from notana_planner.week import plan_week, validate_week, week_summary

CFG = ScenarioConfig(seed=11, target_interventions=900, employee_count=20)


@pytest.fixture(scope="module")
def week():
    scs = generate_week(CFG)
    w0 = World.create(scs[0])
    worlds = [w0] + [World(sc, w0.base_matrix, weights=ObjectiveWeights(), settings=SolverSettings()) for sc in scs[1:]]
    plans = plan_week(worlds, time_limit_s=3)
    return scs, plans


def test_roster_two_consecutive_days_off_and_same_staff_per_day():
    scs = generate_week(CFG)
    assert [sc.weekday for sc in scs] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert all(len(sc.employees) == CFG.employee_count for sc in scs)
    staff = {}
    for sc in scs:
        for e in sc.employees.values():
            staff.setdefault(e.id, set()).add(sc.day)
    for eid, days in staff.items():
        off = set(range(7)) - days
        assert len(off) == 2 and any((d + 1) % 7 in off for d in off), (eid, off)


def test_weekly_frequencies_are_respected():
    scs = generate_week(CFG)
    for key, k in WEEKLY_FREQUENCY.items():
        per_recipient = {}
        for sc in scs:
            for i in sc.interventions.values():
                if i.type == key:
                    per_recipient.setdefault(i.recipient_id, set()).add(sc.day)
        assert all(len(days) <= k for days in per_recipient.values()), key


def test_week_plan_valid_and_week_rules_hold(week):
    scs, plans = week
    assert all(p.validation["valid"] for p in plans)
    rep = validate_week(scs, plans)
    assert rep.valid, rep.errors[:3]
    assert rep.checks > len(scs[0].employees)
    s = week_summary(scs, plans)
    assert s["totals"]["days_planned"] == 7 and s["totals"]["planned"] > 0.8 * s["totals"]["visits"]


def test_continuity_carries_over_days(week):
    scs, plans = week
    # Someone who served a recipient on Monday is "known" on Sunday.
    a = next(iter(plans[0].assignments.values()))
    rid = scs[0].visits[a.visit_id].recipient_id
    assert set(a.employee_ids) <= set(scs[6].recipients[rid].known_employee_ids)


def test_week_validator_detects_short_rest_and_hours(week):
    scs, plans = week
    plans = copy.deepcopy(plans)
    scs2 = copy.deepcopy(scs)
    # An employee working Mon and Tue: let Monday run until 3 h before Tuesday's start.
    eid = next(e for e in scs2[0].employees if e in scs2[1].employees
               and plans[0].routes[e].stops and plans[1].routes[e].stops)
    mon, tue = plans[0].routes[eid], plans[1].routes[eid]
    mon.route_end = 24 * 60 + tue.route_start - 180
    codes = {e.code for e in validate_week(scs2, plans).errors}
    assert "DAILY_REST_VIOLATED" in codes
    for sc in scs2:
        if eid in sc.employees:
            sc.employees[eid].contract_minutes_per_week = 60
    codes = {e.code for e in validate_week(scs2, plans).errors}
    assert "WEEKLY_HOURS_EXCEEDED" in codes


def test_week_validator_detects_missing_weekly_rest(week):
    scs, plans = week
    plans = copy.deepcopy(plans)
    eid = next(iter(scs[0].employees))
    # Pretend the employee also worked their days off, all day long with short nights.
    for d, p in enumerate(plans):
        r = p.routes.get(eid)
        if r is not None and r.stops:
            r.route_start, r.route_end = 6 * 60, 21 * 60
    scs2 = copy.deepcopy(scs)
    for sc, p in zip(scs2, plans):
        if eid not in sc.employees:
            sc.employees[eid] = copy.deepcopy(scs[0].employees[eid])
            src = plans[0].routes[eid]
            p.routes[eid] = copy.deepcopy(src)
            p.routes[eid].route_start, p.routes[eid].route_end = 6 * 60, 21 * 60
    codes = {e.code for e in validate_week(scs2, plans).errors}
    assert "WEEKLY_REST_VIOLATED" in codes
