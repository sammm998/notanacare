"""Machine learning: counterfactual cases, training, cross-validated evaluation, strategy E."""

import copy

from notana_planner.incidents import Incident, apply_incident
from notana_planner.ml.cases import ACTIONS, load_cases, make_case, score
from notana_planner.ml.features import FEATURES, case_features
from notana_planner.ml.model import StrategySelector, evaluate, similar_cases
from notana_planner.replanning import replan_with_strategy


def _sick(plan, clock=600):
    eid = max(plan.routes, key=lambda e: (sum(1 for s in plan.routes[e].stops if s.kind == "visit" and s.start > clock), e))
    return Incident("employee_sick", clock, {"employee_ids": [eid]})


def test_features_are_complete_and_numeric(small_world, small_plan):
    w = copy.deepcopy(small_world)
    eff = apply_incident(w, small_plan, _sick(small_plan))
    f = case_features(w, small_plan, eff)
    assert list(f) == list(FEATURES)
    assert f["kind_employee_sick"] == 1.0 and f["released"] > 0 and f["load_ratio"] > 0


def test_case_runs_every_action_on_the_same_situation(small_world, small_plan):
    c = make_case(small_world, small_plan, _sick(small_plan), "test")
    assert set(c["outcomes"]) == set(ACTIONS)
    for o in c["outcomes"].values():
        assert o["score"] == round(score(o), 2)
    assert c["outcomes"][c["best"]]["score"] == min(o["score"] for o in c["outcomes"].values())
    # The world the case was drawn from is untouched (clones per action).
    assert all(e.status.value == "working" for e in small_world.scenario.employees.values())


def test_selector_trains_evaluates_and_predicts():
    cases = load_cases()
    assert len(cases) >= 20, "bundled simulated cases are missing"
    sel = StrategySelector()
    info = sel.train(evaluate_cv=False)
    assert info["trained"] and info["cases"] == len(cases)
    p = sel.predict(cases[0]["features"])
    assert p["action"] in ACTIONS and set(p["predicted_gain_vs_default"]) == set(ACTIONS) - {"local"}
    ev = evaluate(cases, folds=4)
    assert ev["non_default_choices"] >= 0 and len(ev["margins_chosen"]) == 4
    assert ev["mean_score"]["oracle"] <= min(ev["mean_score"]["learned"], ev["mean_score"]["default"])
    assert sum(ev["chosen_action_counts"].values()) == len(cases)
    sim = similar_cases(cases[0]["features"], cases, k=3)
    assert len(sim) == 3 and sim[0]["distance"] == 0.0


def test_learned_strategy_replans_validly(small_world, small_plan):
    w = copy.deepcopy(small_world)
    eff = apply_incident(w, small_plan, _sick(small_plan))
    res, meta = replan_with_strategy(w, small_plan, eff, "learned")
    assert res.plan.validation["valid"]
    assert "learned" in meta and (not meta["learned"]["used"] or meta["learned"]["action"] in ACTIONS)
