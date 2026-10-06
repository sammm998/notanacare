"""Live simulation: the day runs, things happen, the plan reacts.

The clock advances from ``start`` to ``end``. Events arrive at random (but
seeded, so a run can be reproduced) moments: safety alarms, a sick child to
pick up (VAB), delays, traffic, sickness, cancellations and moved medication.
Each event is applied to the world and re-planned immediately with fast local
repair; every result is validated independently. The feed records what
happened, who was dispatched or moved, what became unplanned and the best
suggestion for it.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

from .domain import EmployeeStatus, Plan, fmt_time
from .incidents import Incident, apply_incident
from .planner import World
from .replanning import RepairParams, replan
from .suggestions import public, relaxed_options

EVENT_WEIGHTS = (
    ("alarm", 0.28),
    ("child_sick", 0.16),
    ("employee_delayed", 0.18),
    ("traffic", 0.10),
    ("employee_sick", 0.08),
    ("visit_cancelled", 0.12),
    ("medication_moved", 0.08),
)
LABELS = {
    "alarm": "Safety alarm",
    "child_sick": "Child sick (VAB)",
    "employee_delayed": "Employee delayed",
    "traffic": "Traffic disruption",
    "employee_sick": "Employee sick",
    "visit_cancelled": "Visit cancelled",
    "medication_moved": "Medication time moved",
}


def fast_params() -> RepairParams:
    p = RepairParams()
    p.phase1_time_s, p.phase2_time_s, p.max_phase = 3.0, 5.0, 2
    return p


def _employees_with_future(plan: Plan, world: World, clock: int, n: int = 2) -> list[str]:
    out = []
    for eid, r in plan.routes.items():
        e = world.scenario.employees.get(eid)
        if e is None or e.status != EmployeeStatus.WORKING or any(iv.start <= clock < iv.end for iv in e.unavailable):
            continue
        if sum(1 for s in r.stops if s.kind == "visit" and s.start > clock) >= n:
            out.append(eid)
    return sorted(out)


def make_event(rng: random.Random, world: World, plan: Plan, clock: int, kind: str | None = None) -> Incident | None:
    sc = world.scenario
    kinds = [k for k, _ in EVENT_WEIGHTS]
    kind = kind or rng.choices(kinds, weights=[w for _, w in EVENT_WEIGHTS])[0]
    emps = _employees_with_future(plan, world, clock)
    if kind == "alarm":
        busy = {sc.visits[v].recipient_id for v, a in plan.assignments.items() if v in sc.visits and clock - 40 < a.start < clock + 90}
        cands = sorted(r for r in sc.recipients if r not in busy)
        if not cands:
            return None
        return Incident("alarm", clock, {"recipient_id": rng.choice(cands), "duration": rng.choice((15, 20, 25, 30))})
    if kind in ("child_sick", "employee_sick", "employee_delayed"):
        if not emps:
            return None
        eid = rng.choice(emps)
        if kind == "child_sick":
            return Incident(kind, clock, {"employee_id": eid, "return_minutes": rng.choice((None, None, 120, 180))})
        if kind == "employee_sick":
            return Incident(kind, clock, {"employee_ids": [eid]})
        return Incident(kind, clock, {"employee_id": eid, "minutes": rng.choice((15, 20, 30, 40))})
    if kind == "traffic":
        zone = rng.choice(sorted({sc.recipients[r].zone for r in sc.recipients}))
        return Incident(kind, clock, {"zone": zone, "multiplier": rng.choice((1.4, 1.6, 1.8))})
    if kind == "visit_cancelled":
        fut = sorted(v for v, a in plan.assignments.items() if a.start > clock + 30 and v in sc.visits
                     and not v.startswith("ALARM"))
        if not fut:
            return None
        return Incident(kind, clock, {"visit_id": rng.choice(fut), "reason": rng.choice(
            ("recipient in hospital", "family visiting", "recipient declined"))})
    if kind == "medication_moved":
        meds = []
        for v, a in plan.assignments.items():
            vv = sc.visits.get(v)
            if vv is None or a.start <= clock + 45:
                continue
            if any(sc.interventions[i].type in ("medication", "insulin") for i in vv.intervention_ids):
                meds.append((v, a.start))
        if not meds:
            return None
        v, st = rng.choice(sorted(meds))
        return Incident(kind, clock, {"visit_id": v, "new_time": max(clock + 60, st + rng.choice((-60, -30, 30, 60)))})
    return None


def run_live(
    world: World,
    plan: Plan,
    start: int = 7 * 60 + 30,
    end: int = 20 * 60,
    events: int = 8,
    seed: int = 7,
    pace_s: float = 1.0,
    on_event: Callable[[dict, Plan], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_clock: Callable[[int], None] | None = None,
) -> tuple[Plan, list[dict]]:
    rng = random.Random(seed)
    if plan.clock is not None:
        start = max(start, plan.clock + 1)
    times = sorted(rng.randint(start, end) for _ in range(events))
    feed: list[dict] = []
    cur = plan
    for k, clock in enumerate(times):
        if should_stop and should_stop():
            break
        if on_clock:
            on_clock(clock)
        # Draw until an event applies (e.g. an alarm at a recipient whose visit is in
        # progress is rejected), so the requested number of events really happens.
        inc = effect = None
        before_unplanned = set(cur.unplanned)
        t0 = time.perf_counter()
        for _ in range(12):
            cand = make_event(rng, world, cur, clock)
            if cand is None:
                continue
            try:
                effect = apply_incident(world, cur, cand)
                inc = cand
                break
            except ValueError:
                continue
        if inc is None or effect is None:
            feed.append({"n": k + 1, "clock": clock, "kind": "none", "label": "No event",
                         "title": "no applicable event at this time", "skipped": True})
            continue
        res = replan(world, cur, effect, fast_params(), "live")
        new = res.plan
        new.strategy = f"live: {LABELS.get(inc.kind, inc.kind)}"
        newly = sorted(v for v in new.unplanned if v not in before_unplanned)
        hints = []
        for vid in newly[:3]:
            v = world.scenario.visits.get(vid)
            if v is None:
                continue
            opts = relaxed_options(world, new, v, limit=1)
            if opts:
                hints.append({"visit_id": vid, "best": public(opts[0])})
        entry: dict[str, Any] = {
            "n": k + 1,
            "clock": clock,
            "clock_text": fmt_time(clock),
            "kind": inc.kind,
            "label": LABELS.get(inc.kind, inc.kind),
            "incident": inc.to_dict(),
            "title": effect.description,
            "summary": res.summary,
            "strategy_used": res.strategy_used,
            "visits_changed": res.diff["counts"]["visits_changed"],
            "routes_unchanged": res.diff["counts"].get("routes_unchanged"),
            "newly_unplanned": newly,
            "unplanned_total": len(new.unplanned),
            "interventions_planned_share": new.score.get("interventions_planned_share"),
            "valid": new.validation.get("valid"),
            "status": new.validation.get("status"),
            "seconds": round(time.perf_counter() - t0, 1),
            "plan_id": new.id,
            "before_plan_id": cur.id,
            "suggestions": hints,
        }
        if inc.kind == "alarm":
            entry["response_minutes"] = effect.facts.get("response_minutes")
            entry["dispatched"] = effect.facts.get("dispatched")
        feed.append(entry)
        cur = new
        if on_event:
            on_event(entry, new)
        if pace_s:
            time.sleep(pace_s)
    if on_clock:
        on_clock(end)
    return cur, feed
