"""The validator must independently catch every class of hard-constraint violation."""

import copy

from notana_planner.validator import validate_plan


def codes(world, plan, scenario=None, **kw):
    rep = validate_plan(scenario or world.scenario, world.travel(), plan, **kw)
    return {e.code for e in rep.errors}, rep


def visit_stops(plan):
    for eid, r in plan.routes.items():
        for i, s in enumerate(r.stops):
            if s.kind == "visit":
                yield eid, i, s


def test_optimized_plan_is_valid(small_world, small_plan):
    c, rep = codes(small_world, small_plan)
    assert rep.valid, rep.errors[:5]
    assert rep.checks["travel"] > 0 and rep.checks["time_window"] > 0


def test_detects_shortened_duration(small_world, plan_copy):
    eid, i, s = next(visit_stops(plan_copy))
    s.end -= 5
    c, _ = codes(small_world, plan_copy)
    assert "DURATION_NOT_RESERVED" in c


def test_detects_overlap_or_travel_violation(small_world, plan_copy):
    for eid, r in plan_copy.routes.items():
        vs = [s for s in r.stops if s.kind == "visit"]
        if len(vs) >= 2:
            a, b = vs[0], vs[1]
            dur = b.end - b.start
            b.start = a.start + 1
            b.end = b.start + dur
            plan_copy.assignments[b.visit_id].start = b.start
            break
    c, _ = codes(small_world, plan_copy)
    assert c & {"OVERLAP", "TRAVEL_INFEASIBLE", "ROUTE_NOT_CHRONOLOGICAL"}


def test_detects_time_window_violation(small_world, plan_copy):
    eid, i, s = next((e, i, s) for e, i, s in visit_stops(plan_copy)
                     if small_world.scenario.visits[s.visit_id].required_employee_count == 1)
    v = small_world.scenario.visits[s.visit_id]
    dur = s.end - s.start
    s.start = v.latest_start + 40
    s.end = s.start + dur
    plan_copy.assignments[s.visit_id].start = s.start
    c, _ = codes(small_world, plan_copy)
    assert "TIME_WINDOW_VIOLATED" in c


def test_detects_missing_delegation_and_skill(small_world, small_plan):
    sc = copy.deepcopy(small_world.scenario)
    vid, a = next((vid, a) for vid, a in small_plan.assignments.items() if sc.visits[vid].required_delegations)
    for eid in a.employee_ids:
        sc.employees[eid].delegations = []
    c, _ = codes(small_world, small_plan, scenario=sc)
    assert "MISSING_DELEGATION" in c

    sc2 = copy.deepcopy(small_world.scenario)
    vid, a = next((vid, a) for vid, a in small_plan.assignments.items() if sc2.visits[vid].required_skills)
    sc2.employees[a.employee_ids[0]].skills = []
    c2, _ = codes(small_world, small_plan, scenario=sc2)
    assert "MISSING_SKILL" in c2


def test_detects_unsynchronised_double_staffing(small_world, plan_copy):
    doubles = [vid for vid, a in plan_copy.assignments.items() if len(a.employee_ids) == 2]
    assert doubles, "scenario should contain planned double-staffed visits"
    vid = doubles[0]
    eid = plan_copy.assignments[vid].employee_ids[1]
    st = next(s for s in plan_copy.routes[eid].stops if s.visit_id == vid)
    st.start += 7
    st.end += 7
    c, _ = codes(small_world, plan_copy)
    assert "DOUBLE_STAFFING_NOT_SYNCHRONISED" in c


def test_detects_wrong_staff_count(small_world, plan_copy):
    vid = next(vid for vid, a in plan_copy.assignments.items() if len(a.employee_ids) == 2)
    eid = plan_copy.assignments[vid].employee_ids.pop()
    plan_copy.routes[eid].stops = [s for s in plan_copy.routes[eid].stops if s.visit_id != vid]
    c, _ = codes(small_world, plan_copy)
    assert "WRONG_STAFF_COUNT" in c


def test_detects_silently_dropped_visit(small_world, plan_copy):
    eid, i, s = next(visit_stops(plan_copy))
    vid = s.visit_id
    for r in plan_copy.routes.values():
        r.stops = [x for x in r.stops if x.visit_id != vid]
    del plan_copy.assignments[vid]
    c, _ = codes(small_world, plan_copy)
    assert "VISIT_MISSING" in c


def test_detects_moved_started_visit(small_world, small_plan, plan_copy):
    vid, a = min(small_plan.assignments.items(), key=lambda x: x[1].start)
    clock = a.start + 1
    pa = plan_copy.assignments[vid]
    pa.start += 3
    pa.end += 3
    c, _ = codes(small_world, plan_copy, reference=small_plan, clock=clock)
    assert "LOCKED_VISIT_MOVED" in c


def test_detects_shift_and_break_violations(small_world, plan_copy):
    eid, r = next((e, r) for e, r in plan_copy.routes.items()
                  if any(s.kind == "break" for s in r.stops) and small_world.scenario.employees[e].breaks)
    r.stops = [s for s in r.stops if s.kind != "break"]
    c, _ = codes(small_world, plan_copy)
    assert "BREAK_MISSING" in c


def test_detects_avoided_employee(small_world, small_plan):
    sc = copy.deepcopy(small_world.scenario)
    vid, a = next(iter(small_plan.assignments.items()))
    sc.recipients[sc.visits[vid].recipient_id].avoid_employee_ids.append(a.employee_ids[0])
    c, _ = codes(small_world, small_plan, scenario=sc)
    assert "AVOIDED_EMPLOYEE" in c


def test_detects_work_outside_shift(small_world, plan_copy):
    eid, r = next((e, r) for e, r in plan_copy.routes.items() if r.route_end is not None)
    r.route_end = small_world.scenario.employees[eid].shift_end + 30
    c, _ = codes(small_world, plan_copy)
    assert "OUTSIDE_SHIFT" in c


def _first_assignment(small_world, small_plan):
    vid, a = next(iter(sorted(small_plan.assignments.items())))
    sc = copy.deepcopy(small_world.scenario)
    return sc, vid, a, sc.recipients[sc.visits[vid].recipient_id], sc.employees[a.employee_ids[0]]


def test_detects_strict_gender_violation(small_world, small_plan):
    sc, vid, a, r, e = _first_assignment(small_world, small_plan)
    r.gender_preference, r.gender_strict, r.gender_scope = ("M" if e.gender == "F" else "F"), True, "all"
    c, _ = codes(small_world, small_plan, scenario=sc)
    assert "GENDER_REQUIREMENT_VIOLATED" in c
    r.gender_strict = False  # a wish is soft: never a hard violation
    c, _ = codes(small_world, small_plan, scenario=sc)
    assert "GENDER_REQUIREMENT_VIOLATED" not in c


def test_detects_pet_allergy_smoking_and_language(small_world, small_plan):
    sc, vid, a, r, e = _first_assignment(small_world, small_plan)
    r.pets, e.pet_allergies = ["cat"], ["cat"]
    r.smokes, e.avoid_smoking = True, True
    r.languages, r.language_required = ["so"], True
    e.languages = ["sv"]
    c, _ = codes(small_world, small_plan, scenario=sc)
    assert {"PET_ALLERGY", "SMOKING_EXPOSURE", "LANGUAGE_REQUIRED"} <= set(c)


def test_optimizer_respects_hard_wishes(small_world):
    """Make wishes binding for a few recipients and re-optimise: the plan stays valid
    and nobody excluded by a hard wish is assigned."""
    from notana_planner.planner import World, plan_day

    sc = copy.deepcopy(small_world.scenario)
    emps = sorted(sc.employees.values(), key=lambda e: e.id)
    for r in sorted(sc.recipients.values(), key=lambda r: r.id)[:12]:
        r.gender_preference, r.gender_strict, r.gender_scope = "F", True, "all"
        r.pets = ["dog"]
    for e in emps[::4]:
        e.pet_allergies = ["dog"]
    w = World(sc, small_world.base_matrix, weights=small_world.weights, settings=small_world.settings)
    plan = plan_day(w, time_limit_s=6)
    rep = validate_plan(sc, w.travel(), plan)
    assert rep.valid, rep.errors[:3]
    for vid, a in plan.assignments.items():
        r = sc.recipients[sc.visits[vid].recipient_id]
        for eid in a.employee_ids:
            if r.gender_strict:
                assert sc.employees[eid].gender == "F"
            assert not set(r.pets) & set(sc.employees[eid].pet_allergies)
