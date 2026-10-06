"""Simulated training cases: every candidate strategy on the same incident.

A case = (scenario, plan, incident). The world is cloned once per action, so
all actions face exactly the same situation (counterfactual evaluation). The
outcome of each action is measured from the plan it produces and the
independent validator; the *score* (lower = better) is

    1000 * lost high-priority + 100 * lost + 20 * extra unplanned
    + 1 * visits changed + 0.5 * seconds        (invalid plan: 1e6)

which mirrors the re-planning acceptance order: broken promises first, then
unplanned visits, then stability, then runtime.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Callable

from ..domain import ScenarioConfig
from ..generator import generate_scenario
from ..incidents import apply_incident
from ..planner import World, plan_day
from ..replanning import RepairParams, clone_world, replan
from .features import case_features

ACTIONS: dict[str, dict[str, Any]] = {
    "local": {},  # default: local repair, escalate when needed
    "local_enhanced": {"enhanced_repair": True},
    "expanded": {"start_phase": 2},
    "broad": {"start_phase": 3},
}
DEFAULT_ACTION = "local"

ROOT = Path(__file__).resolve().parent
BUNDLED = ROOT / "data" / "replan_cases.jsonl"


def action_params(name: str, fast: bool = False) -> RepairParams:
    p = RepairParams()
    if fast:
        p.phase1_time_s, p.phase2_time_s, p.phase3_time_s = 3.0, 5.0, 8.0
    for k, v in ACTIONS[name].items():
        setattr(p, k, v)
    p.source = f"ml-action:{name}"
    return p


def score(outcome: dict) -> float:
    if not outcome["valid"]:
        return 1e6
    return (1000 * outcome["lost_high_priority"] + 100 * outcome["lost"] + 20 * outcome["extra_unplanned"]
            + outcome["visits_changed"] + 0.5 * outcome["seconds"])


def run_action(world: World, plan, incident, action: str, fast: bool = True) -> dict:
    w = clone_world(world)
    effect = apply_incident(w, plan, incident)
    t0 = time.perf_counter()
    res = replan(w, plan, effect, action_params(action, fast), f"ml:{action}")
    dt = time.perf_counter() - t0
    vis = w.scenario.visits
    lost = [
        v for v in plan.assignments
        if v in vis and v not in res.plan.assignments and vis[v].status.value != "cancelled" and vis[v].latest_start >= effect.clock
    ]
    out = {
        "valid": bool(res.plan.validation.get("valid")),
        "lost": len(lost),
        "lost_high_priority": sum(1 for v in lost if vis[v].priority >= 5),
        "extra_unplanned": len(res.plan.unplanned) - len(plan.unplanned),
        "visits_changed": res.diff["counts"]["visits_changed"],
        "seconds": round(dt, 2),
        "phase_used": res.strategy_used,
    }
    out["score"] = round(score(out), 2)
    return out


def make_case(world: World, plan, incident, source: str, fast: bool = True) -> dict:
    w = clone_world(world)
    effect = apply_incident(w, plan, incident)
    feats = case_features(w, plan, effect)
    outcomes = {a: run_action(world, plan, incident, a, fast) for a in ACTIONS}
    best = min(outcomes, key=lambda a: (outcomes[a]["score"], a != DEFAULT_ACTION))
    return {"source": source, "incident": incident.to_dict(), "features": feats, "outcomes": outcomes,
            "best": best, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}


def generate_cases(world: World, plan, n: int, seed: int, source: str,
                   progress: Callable[[str], None] | None = None, fast: bool = True) -> list[dict]:
    """``n`` cases on this world/plan: random (seeded) incidents at random times."""
    from ..live import make_event

    rng = random.Random(seed)
    cases = []
    tries = 0
    while len(cases) < n and tries < n * 6:
        tries += 1
        clock = rng.randint(7 * 60 + 30, 19 * 60)
        inc = make_event(rng, world, plan, clock)
        if inc is None:
            continue
        try:
            cases.append(make_case(world, plan, inc, source, fast))
        except ValueError:
            continue  # e.g. alarm at a recipient with a visit in progress
        if progress:
            c = cases[-1]
            progress(f"case {len(cases)}/{n}: {inc.kind} at {clock // 60:02d}:{clock % 60:02d} -> best '{c['best']}'")
    return cases


def scenario_cases(seed: int, n: int, size: tuple[int, int] = (1000, 22)) -> list[dict]:
    sc = generate_scenario(ScenarioConfig(seed=seed, target_interventions=size[0], employee_count=size[1]))
    w = World.create(sc)
    plan = plan_day(w, "baseline", 6)
    return generate_cases(w, plan, n, seed * 101, f"sim:seed={seed},iv={size[0]},emp={size[1]}")


def load_cases(path: Path | None = None) -> list[dict]:
    paths = [BUNDLED] + ([path] if path else [])
    out = []
    for p in paths:
        if p and p.exists():
            out += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return out


def append_cases(path: Path, cases: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for c in cases:
            fh.write(json.dumps(c) + "\n")
