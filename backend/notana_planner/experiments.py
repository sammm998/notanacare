"""Algorithm experiment mode: same scenario, same incidents, different strategies.

All strategies share the seed, visits, employees, travel matrix, the same
baseline day plan and the same incident sequence. Only the re-planning
strategy differs. Every resulting plan is independently validated; the
comparison reports measured numbers only and states plainly when a strategy
made no difference or was not available (e.g. no ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .domain import Plan, ScenarioConfig
from .generator import generate_scenario
from .incidents import Incident, apply_incident
from .planner import World, plan_day
from .replanning import clone_world, replan_with_strategy

STRATEGY_LABELS = {
    "baseline": "A. Baseline optimizer",
    "enhanced": "B. + enhanced repair",
    "advisor": "C. + planning advisor",
    "classifier": "D. + classifier ranking",
}


@dataclass
class ExperimentSpec:
    config: ScenarioConfig
    incidents: list[Incident]
    strategies: list[str] = field(default_factory=lambda: ["baseline", "enhanced", "advisor", "classifier"])
    base_time_limit_s: float = 25.0
    skip_llm_without_key: bool = False


def default_incidents(plan: Plan, scenario: Any, clock: int = 10 * 60 + 14, n_sick: int = 2) -> list[Incident]:
    """Deterministic demo sequence derived from the baseline plan."""
    busiest = sorted(
        plan.routes,
        key=lambda e: (-sum(1 for s in plan.routes[e].stops if s.kind == "visit" and s.start > clock), e),
    )
    med_visit = next(
        (
            vid
            for vid, a in sorted(plan.assignments.items(), key=lambda x: (x[1].start, x[0]))
            if a.start > 11 * 60 + 15
            and any(scenario.interventions[i].type in ("medication", "insulin") for i in scenario.visits[vid].intervention_ids)
        ),
        None,
    )
    seq = [Incident("employee_sick", clock, {"employee_ids": busiest[:n_sick]})]
    seq.append(Incident("traffic", clock + 20, {"zone": "Södermalm", "multiplier": 1.6}))
    seq.append(Incident("employee_delayed", clock + 30, {"employee_id": busiest[n_sick], "minutes": 25}))
    if med_visit:
        seq.append(Incident("medication_moved", clock + 40, {"visit_id": med_visit, "new_time": 11 * 60}))
    return seq


def _metrics(plan: Plan, base: Plan) -> dict[str, Any]:
    s = plan.score
    stab = s.get("stability", {})
    lost = [v for v in base.assignments if v not in plan.assignments]
    return {
        "planned_visits": s["planned_visits"],
        "unplanned_visits": s["unplanned_visits"],
        "unplanned_high_priority": s["unplanned_high_priority"],
        "hard_violations": plan.validation.get("error_count", 0),
        "valid": plan.validation.get("valid"),
        "travel_minutes": s["travel_minutes"],
        "preferred_time_deviation_avg": s["preferred_time_deviation_avg"],
        "continuity_known_share": s["continuity_known_share"],
        "lost_vs_base": len(lost),
        "changed_assignments_last_step": stab.get("employee_changes"),
        "changed_start_times_last_step": stab.get("start_time_changes"),
    }


def run_experiment(spec: ExperimentSpec, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    say = progress or (lambda m: None)
    t0 = time.perf_counter()
    scenario = generate_scenario(spec.config)
    world0 = World.create(scenario)
    say("baseline day plan")
    base = plan_day(world0, "baseline", spec.base_time_limit_s)
    incidents = spec.incidents or default_incidents(base, scenario)
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    results = []
    for strat in spec.strategies:
        note = ""
        if strat == "advisor" and not has_key:
            if spec.skip_llm_without_key:
                results.append({"strategy": strat, "label": STRATEGY_LABELS[strat], "skipped": True,
                                "note": "skipped: ANTHROPIC_API_KEY not set"})
                continue
            note = "ANTHROPIC_API_KEY not set: ran the deterministic advisor fallback (no LLM involved)"
        if strat == "classifier":
            note = "mock deterministic classifier (no verified JEV implementation available)"
        say(f"strategy {strat}")
        w = clone_world(world0)
        plan = base
        steps = []
        runtime = 0.0
        cost = 0.0
        for inc in incidents:
            eff = apply_incident(w, plan, inc)
            ts = time.perf_counter()
            res, meta = replan_with_strategy(w, plan, eff, strat)
            dt = time.perf_counter() - ts
            runtime += dt
            usage = (meta.get("advisor") or {}).get("usage") or {}
            cost += float(usage.get("cost_usd", 0.0) or 0.0)
            steps.append(
                {
                    "incident": inc.to_dict(),
                    "description": eff.description,
                    "phase_used": res.strategy_used,
                    "seconds": round(dt, 2),
                    "visits_changed": res.diff["counts"]["visits_changed"],
                    "newly_unplanned": res.diff["counts"]["newly_unplanned"],
                    "valid": res.plan.validation.get("valid"),
                    "advisor": meta.get("advisor"),
                    "classification": meta.get("classification"),
                }
            )
            plan = res.plan
        m = _metrics(plan, base)
        m["changed_assignments_total"] = sum(
            1 for v, a in base.assignments.items()
            if v in plan.assignments and sorted(a.employee_ids) != sorted(plan.assignments[v].employee_ids)
        )
        m["changed_start_times_total"] = sum(
            1 for v, a in base.assignments.items() if v in plan.assignments and a.start != plan.assignments[v].start
        )
        m["solver_runtime_s"] = round(runtime, 2)
        m["api_cost_usd"] = round(cost, 4)
        results.append({"strategy": strat, "label": STRATEGY_LABELS.get(strat, strat), "metrics": m,
                        "steps": steps, "note": note, "skipped": False})
    return {
        "scenario": {"id": scenario.id, "seed": spec.config.seed, "visits": len(scenario.visits),
                     "employees": len(scenario.employees), "recipients": len(scenario.recipients),
                     "interventions": len(scenario.interventions)},
        "baseline_plan": {"planned": base.score["planned_visits"], "unplanned": base.score["unplanned_visits"],
                          "valid": base.validation.get("valid")},
        "incidents": [i.to_dict() for i in incidents],
        "results": results,
        "conclusion": _conclusion(results),
        "seconds": round(time.perf_counter() - t0, 1),
        "llm_available": has_key,
    }


def _conclusion(results: list[dict]) -> list[str]:
    ran = [r for r in results if not r.get("skipped")]
    base = next((r for r in ran if r["strategy"] == "baseline"), None)
    if base is None:
        return ["No baseline run to compare against."]
    b = base["metrics"]
    out = []
    if not b["valid"]:
        out.append(f"{base['label']}: final plan INVALID ({b['hard_violations']} hard violations) - see per-incident detail.")
    for r in ran:
        if r is base:
            continue
        m = r["metrics"]
        d_unpl = m["unplanned_visits"] - b["unplanned_visits"]
        d_chg = m["changed_assignments_total"] - b["changed_assignments_total"]
        d_tr = m["travel_minutes"] - b["travel_minutes"]
        if d_unpl == 0 and abs(d_chg) <= 1 and abs(d_tr) < 30:
            verdict = "no measurable difference from the baseline on this scenario"
        else:
            parts = []
            if d_unpl:
                parts.append(f"{abs(d_unpl)} {'fewer' if d_unpl < 0 else 'more'} unplanned visits")
            if d_chg:
                parts.append(f"{abs(d_chg)} {'fewer' if d_chg < 0 else 'more'} reassigned visits")
            if abs(d_tr) >= 30:
                parts.append(f"{abs(d_tr)} min {'less' if d_tr < 0 else 'more'} travel")
            verdict = ", ".join(parts)
        if not m["valid"]:
            verdict += " (final plan INVALID)"
        extra = f"; {r['note']}" if r.get("note") else ""
        out.append(f"{r['label']}: {verdict} (runtime {m['solver_runtime_s']}s vs {b['solver_runtime_s']}s, API cost ${m['api_cost_usd']}){extra}.")
    out.append("Single scenario, single seed: differences are indicative, not statistically established.")
    return out
