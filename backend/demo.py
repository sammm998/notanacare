"""Headless demo: generate -> optimise -> validate -> incidents -> re-plan.

    python demo.py                       # default Stockholm scenario
    python demo.py --seed 7 --employees 90 --time-limit 30
    python demo.py --sick 3 --clock 10:14
"""

from __future__ import annotations

import argparse
import time

from notana_planner.domain import ScenarioConfig, fmt_time
from notana_planner.explain import explain_plan_text
from notana_planner.generator import generate_scenario
from notana_planner.incidents import Incident, apply_incident
from notana_planner.planner import World, plan_day
from notana_planner.replanning import replan_with_strategy


def hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--interventions", type=int, default=5000)
    ap.add_argument("--employees", type=int, default=100)
    ap.add_argument("--pressure", type=float, default=0.3)
    ap.add_argument("--time-limit", type=float, default=25)
    ap.add_argument("--clock", default="10:14")
    ap.add_argument("--sick", type=int, default=1, help="number of employees calling in sick")
    ap.add_argument("--strategy", default="baseline", choices=["baseline", "enhanced", "advisor", "classifier"])
    a = ap.parse_args()

    t0 = time.time()
    sc = generate_scenario(ScenarioConfig(seed=a.seed, target_interventions=a.interventions,
                                          employee_count=a.employees, staffing_pressure=a.pressure))
    print(f"Scenario {sc.id}: {len(sc.interventions)} interventions -> {len(sc.visits)} visits, "
          f"{len(sc.recipients)} recipients, {len(sc.employees)} employees ({time.time() - t0:.1f}s)")
    world = World.create(sc)
    print(f"Travel source: {world.travel().source}")
    plan = plan_day(world, a.strategy, a.time_limit)
    print("\n== Day plan ==")
    print(explain_plan_text(plan))
    clock = hhmm(a.clock)
    busiest = sorted(plan.routes, key=lambda e: -sum(1 for s in plan.routes[e].stops if s.kind == "visit" and s.start > clock))
    inc = Incident("employee_sick", clock, {"employee_ids": busiest[: a.sick]})
    effect = apply_incident(world, plan, inc)
    res, _ = replan_with_strategy(world, plan, effect, a.strategy)
    print(f"\n== Incident at {fmt_time(clock)} ==")
    print(res.summary)
    print(f"\nVisits changed: {res.diff['counts']['visits_changed']} · routes untouched: {res.diff['counts']['routes_unchanged']}")
    print(f"Total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
