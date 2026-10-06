"""Linear-time insertion check and demand-based staffing."""

import random

from notana_planner import catalog as C
from notana_planner.domain import ScenarioConfig
from notana_planner.generator import generate_scenario
from notana_planner.solver.builder import build_global_problem
from notana_planner.solver.construction import Constructor
from notana_planner.solver.routes import Stop, insertion_options, insertion_options_reference


def _key(opts):
    return [(o.position, o.lo, o.hi, o.delta_travel) for o in opts]


def test_fast_insertion_matches_reference(small_world):
    w = small_world
    problem = build_global_problem(w.scenario, w.travel(), w.weights, w.settings)
    ctor = Constructor(problem)
    ctor.run()
    rng = random.Random(1)
    checked = 0
    for t in rng.sample(problem.tasks, 60):
        stop = Stop("visit", t.visit_id + "#x", 0, t.location_id, t.duration, t.earliest, t.latest)
        for eid in t.allowed[0][:8]:
            r = ctor.routes[eid]
            assert _key(insertion_options(r, stop, problem.travel)) == _key(insertion_options_reference(r, stop, problem.travel))
            checked += 1
            # The cache must notice a changed route.
            if r.stops:
                saved = list(r.stops)
                r.stops.pop(0)
                assert _key(insertion_options(r, stop, problem.travel)) == _key(insertion_options_reference(r, stop, problem.travel))
                r.stops[:] = saved
    assert checked > 100


def test_every_team_has_the_shift_mix_and_delegations():
    sc = generate_scenario(ScenarioConfig(seed=42))
    teams = {e.team for e in sc.employees.values()}
    for team in teams:
        staff = [e for e in sc.employees.values() if e.team == team]
        shifts = {e.shift_name for e in staff}
        assert {"early", "late"} <= shifts, (team, shifts)
        for shift in ("early", "late"):
            assert any(C.MEDICATION in e.delegations for e in staff if e.shift_name == shift), (team, shift)
    # Team size follows care minutes.
    minutes = {t: 0 for t in teams}
    for it in sc.interventions.values():
        minutes[sc.recipients[it.recipient_id].zone] += it.duration_minutes
    share = {t: sum(1 for e in sc.employees.values() if e.team == t) / len(sc.employees) for t in teams}
    total = sum(minutes.values())
    for t in teams:
        assert abs(share[t] - minutes[t] / total) < 0.03, (t, share[t], minutes[t] / total)
