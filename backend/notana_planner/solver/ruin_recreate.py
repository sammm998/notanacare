"""Ruin-and-recreate large neighbourhood search on the insertion routes.

Why: on the full day (≈ 600 visits, 100 employees, ~10 000 cross-route
constraints for double staffing and recipient non-overlap) OR-Tools' local
search evaluates only ~15 moves per second and finds no improving solution in
25 s. The exact insertion routines of :mod:`construction` insert a whole day
in well under a second, so ruin-and-recreate on top of them explores far more.

One iteration removes a group of placed visits and re-inserts them, together
with unplanned visits near the ruined area, with the same exact feasibility
checks as the construction (time windows, travel, breaks, synchronised double
staffing, recipient non-overlap). Ruin operators:

* cluster:   a seed visit and its nearest placed neighbours (any employee),
* string:    a run of consecutive visits of one route plus the same time
             slice of a nearby route,
* long legs: the visits reached by the longest trips.

A candidate is kept if its cost is lower (threshold acceptance with a
temperature that falls to zero). Cost = unplanned penalties + travel time and
distance + long-leg penalty + per-assignment costs (continuity, wishes, area).
Pinned visits (started, locked or pinned helpers in re-planning) are never
touched. Hard constraints are never relaxed; the result is re-timed by CP-SAT
and checked by the independent validator like any other plan.
"""

from __future__ import annotations

import random
import time

from .construction import Constructor
from .costs import assignment_penalty
from .lns import _remove
from .problem import PlanningProblem, Task


class _Cost:
    def __init__(self, p: PlanningProblem) -> None:
        self.p = p
        self.w = p.weights
        self._assign: dict[tuple[str, str], int] = {}

    def assign(self, t: Task, eid: str) -> int:
        k = (t.visit_id, eid)
        c = self._assign.get(k)
        if c is None:
            c = self._assign[k] = assignment_penalty(self.p, t, eid)
        return c

    def route(self, r) -> int:  # noqa: ANN001 - WorkRoute
        v = r.vehicle
        tm = v.travel or self.p.travel
        w = self.w
        cur = v.start_location
        total = 0
        legs = [(s.location, s.free_arrival) for s in r.stops] + [(v.end_location, False)]
        if not r.stops:
            return 0
        for loc, free in legs:
            if not free:
                t = tm.minutes(cur, loc)
                total += w.travel_minute * t + int(w.travel_km * tm.km(cur, loc))
                if w.long_leg_minute and t > w.long_leg_threshold_min:
                    total += w.long_leg_minute * (t - w.long_leg_threshold_min)
            cur = loc
        return total

    def total(self, ctor: Constructor, tasks: dict[str, Task]) -> int:
        c = sum(self.route(r) for r in ctor.routes.values() if any(s.kind == "visit" for s in r.stops))
        for vid, pl in ctor.placements.items():
            t = pl.task
            if t.pinned:
                continue
            c += sum(self.assign(t, eid) for eid, _ in pl.roles.values())
        for vid, t in tasks.items():
            if vid not in ctor.placements:
                c += t.penalty * t.roles
        return c


def _snapshot(ctor: Constructor) -> tuple:
    return (
        {e: list(r.stops) for e, r in ctor.routes.items()},
        dict(ctor.placements),
        {k: list(v) for k, v in ctor.recipient_busy.items()},
    )


def _restore(ctor: Constructor, snap: tuple) -> None:
    routes, placements, busy = snap
    for e, stops in routes.items():
        ctor.routes[e].stops = stops
    ctor.placements = placements
    ctor.recipient_busy = busy


def improve(
    problem: PlanningProblem,
    ctor: Constructor,
    time_budget_s: float,
    seed: int = 0,
    max_iterations: int | None = None,
) -> dict:
    t0 = time.perf_counter()
    rng = random.Random(seed)
    tasks = {t.visit_id: t for t in problem.tasks}
    movable = [t for t in problem.tasks if not t.pinned]
    stats = {"iterations": 0, "improvements": 0, "accepted": 0, "seconds": 0.0}
    if not movable or time_budget_s <= 0.05:
        return stats
    tm = problem.travel
    cost = _Cost(problem)
    cur = best = cost.total(ctor, tasks)
    best_snap = _snapshot(ctor)
    start_cost = cur
    start_unplanned = sum(1 for v in tasks if v not in ctor.placements)
    t_start = max(200.0, 0.002 * cur if cur < 10**7 else 2000.0)

    def nearest(loc: str, pool: list[str], k: int) -> list[str]:
        return sorted(pool, key=lambda v: tm.minutes(loc, tasks[v].location_id))[:k]

    while True:
        el = time.perf_counter() - t0
        if el > time_budget_s or (max_iterations is not None and stats["iterations"] >= max_iterations):
            break
        stats["iterations"] += 1
        temp = t_start * max(0.0, 1.0 - el / time_budget_s)
        placed = [v for v, pl in ctor.placements.items() if not pl.task.pinned]
        unplanned = [v for v in tasks if v not in ctor.placements and not tasks[v].pinned]
        if not placed:
            break
        op = rng.random()
        k = rng.randint(6, 18)
        if op < 0.5:
            seed_v = rng.choice(unplanned) if (unplanned and rng.random() < 0.4) else rng.choice(placed)
            ruin = nearest(tasks[seed_v].location_id, placed, k)
            centre = tasks[seed_v].location_id
        elif op < 0.8 and (busy_routes := [
            e for e, r in ctor.routes.items()
            if any(s.kind == "visit" and s.visit_id in ctor.placements and not tasks[s.visit_id].pinned for s in r.stops)
        ]):
            eid = rng.choice(busy_routes)
            vs = [s for s in ctor.routes[eid].stops if s.kind == "visit" and not tasks[s.visit_id].pinned]
            i = rng.randrange(len(vs))
            run = vs[i:i + rng.randint(2, 6)]
            ruin = [s.visit_id for s in run]
            lo, hi = min(tasks[v].earliest for v in ruin), max(tasks[v].latest for v in ruin)
            centre = run[0].location
            others = [v for v in placed if v not in ruin and lo <= tasks[v].latest and tasks[v].earliest <= hi]
            ruin += nearest(centre, others, max(0, k - len(ruin)))
        else:
            legs = []
            for eid, r in ctor.routes.items():
                vtm = r.vehicle.travel or tm
                prev = r.vehicle.start_location
                for s in r.stops:
                    if s.kind == "visit" and s.visit_id in ctor.placements and not tasks[s.visit_id].pinned:
                        legs.append((vtm.minutes(prev, s.location), s.visit_id))
                    prev = s.location
            legs.sort(reverse=True)
            top = [v for _, v in legs[: max(3, k)]]
            pick = rng.choice(top)
            centre = tasks[pick].location_id
            ruin = [pick] + nearest(centre, [v for v in placed if v != pick], k - 1)
        ruin = list(dict.fromkeys(ruin))
        retry = [v for v in unplanned if tm.minutes(centre, tasks[v].location_id) <= 25]
        if len(retry) < 4 and unplanned:
            retry += rng.sample(unplanned, min(len(unplanned), 4))
        snap = _snapshot(ctor)
        for v in ruin:
            _remove(ctor, v)
        order = list(dict.fromkeys(ruin + retry))
        order.sort(key=lambda v: (-tasks[v].priority, -tasks[v].roles, tasks[v].latest - tasks[v].earliest + rng.randint(0, 60)))
        for v in order:
            ctor.try_insert(tasks[v])
        new = cost.total(ctor, tasks)
        if new < cur or new < cur + temp * rng.random():
            stats["accepted"] += 1
            cur = new
            if new < best:
                best = new
                best_snap = _snapshot(ctor)
                stats["improvements"] += 1
        else:
            _restore(ctor, snap)
    _restore(ctor, best_snap)
    stats["seconds"] = round(time.perf_counter() - t0, 2)
    stats["cost_before"] = start_cost
    stats["cost_after"] = best
    stats["unplanned_before"] = start_unplanned
    stats["unplanned_after"] = sum(1 for v in tasks if v not in ctor.placements)
    return stats
