import json
import statistics

from notana_planner.domain import ScenarioConfig, to_jsonable
from notana_planner.generator import generate_scenario
from notana_planner.visit_builder import group_interventions


def test_default_scenario_shape():
    s = generate_scenario(ScenarioConfig())
    visits = list(s.visits.values())
    assert 4900 <= len(s.interventions) <= 5100
    assert 500 <= len(visits) <= 650
    assert 120 <= len(s.recipients) <= 180
    assert len(s.employees) == 100
    per_visit = [len(v.intervention_ids) for v in visits]
    assert min(per_visit) >= 6 and max(per_visit) <= 12
    assert all(4 <= i.duration_minutes <= 10 for i in s.interventions.values())
    by_r = {}
    for v in visits:
        by_r[v.recipient_id] = by_r.get(v.recipient_id, 0) + 1
    assert max(by_r.values()) <= 4
    assert 7 <= statistics.mean(per_visit) <= 11


def test_deterministic_for_same_seed_and_different_for_other_seed():
    a = generate_scenario(ScenarioConfig(seed=5, target_interventions=1200, employee_count=25))
    b = generate_scenario(ScenarioConfig(seed=5, target_interventions=1200, employee_count=25))
    c = generate_scenario(ScenarioConfig(seed=6, target_interventions=1200, employee_count=25))
    dump = lambda s: json.dumps(to_jsonable({"v": s.visits, "e": s.employees, "r": s.recipients}), sort_keys=True)  # noqa: E731
    assert dump(a) == dump(b)
    assert dump(a) != dump(c)


def test_visit_duration_is_sum_and_window_is_intersection():
    s = generate_scenario(ScenarioConfig(seed=3, target_interventions=1000, employee_count=20))
    for v in s.visits.values():
        ivs = [s.interventions[i] for i in v.intervention_ids]
        assert v.total_duration_minutes == sum(i.duration_minutes for i in ivs)
        assert v.earliest_start == max(i.earliest_start for i in ivs)
        assert v.latest_start == min(i.latest_start for i in ivs)
        assert v.earliest_start <= v.latest_start
        assert v.required_employee_count == (2 if any(i.requires_double_staffing for i in ivs) else 1)
        assert set(v.required_delegations) == {d for i in ivs for d in i.required_delegations}


def test_recipient_visits_never_overlap_by_construction():
    s = generate_scenario(ScenarioConfig(seed=8, target_interventions=1500, employee_count=30))
    by_r = {}
    for v in s.visits.values():
        by_r.setdefault(v.recipient_id, []).append(v)
    for vs in by_r.values():
        vs.sort(key=lambda v: v.earliest_start)
        for a, b in zip(vs, vs[1:]):
            assert a.latest_start + a.total_duration_minutes <= b.earliest_start


def test_employee_qualifications_differ_and_shifts_are_inputs():
    s = generate_scenario(ScenarioConfig(seed=1, target_interventions=800, employee_count=40))
    combos = {(tuple(e.skills), tuple(e.delegations)) for e in s.employees.values()}
    assert len(combos) > 10
    assert all(e.shift_end > e.shift_start for e in s.employees.values())


def test_grouping_splits_when_windows_do_not_intersect():
    s = generate_scenario(ScenarioConfig(seed=2, target_interventions=600, employee_count=12))
    items = list(s.interventions.values())[:20]
    groups = group_interventions(items)
    for g in groups:
        assert max(i.earliest_start for i in g) <= min(i.latest_start for i in g)
