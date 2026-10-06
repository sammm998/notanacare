"""Constructive insertion heuristic (first solution + repair).

Inserts tasks one by one at their cheapest feasible position. Double-staffed
tasks are inserted as a *pair*: two different qualified employees whose
feasible start intervals intersect, with the visit fixed at the same start in
both routes. This is what lets the routing local search start from a solution
in which synchronised visits are already active (OR-Tools' own insertion
heuristics insert nodes one at a time and cannot do this).
"""

from __future__ import annotations

from dataclasses import dataclass

from .costs import assignment_penalty, clamp, time_deviation_cost
from .problem import PlanningProblem, Task
from .routes import InsertionOption, Stop, WorkRoute, insertion_options, route_visit_ids


@dataclass(slots=True)
class Placement:
    task: Task
    # role -> (employee_id, start)
    roles: dict[int, tuple[str, int]]


def initial_routes(problem: PlanningProblem) -> dict[str, WorkRoute]:
    routes: dict[str, WorkRoute] = {}
    for v in problem.vehicles:
        stops = [
            Stop("break", None, k, v.break_location, b.duration_minutes, b.earliest_start, b.latest_start,
                 free_arrival=b.kind == "unavailable")
            for k, b in enumerate(v.breaks)
        ]
        routes[v.employee_id] = WorkRoute(v, stops)
    return routes


def _busy_conflict(busy: list[tuple[int, int]], start: int, dur: int) -> tuple[int, int] | None:
    for s, e in busy:
        if start < e and s < start + dur:
            return s, e
    return None


def _pick_time(lo: int, hi: int, target: int, dur: int, busy: list[tuple[int, int]]) -> int | None:
    """Start in [lo, hi] closest to target not overlapping recipient busy intervals."""
    candidates = [clamp(target, lo, hi)]
    for s, e in busy:
        candidates += [e, s - dur]
    best = None
    for t in sorted(set(candidates), key=lambda t: abs(t - target)):
        if lo <= t <= hi and _busy_conflict(busy, t, dur) is None:
            best = t
            break
    return best


def task_order_key(t: Task) -> tuple:
    return (
        t.pinned is None,
        -(t.priority >= 5),
        t.roles == 1,
        t.latest - t.earliest,
        len(t.allowed[0]),
        t.earliest,
        t.visit_id,
    )


class Constructor:
    def __init__(self, problem: PlanningProblem, routes: dict[str, WorkRoute] | None = None) -> None:
        self.p = problem
        self.tm = problem.travel
        self.routes = routes if routes is not None else initial_routes(problem)
        self.recipient_busy: dict[str, list[tuple[int, int]]] = {
            k: list(v) for k, v in problem.recipient_busy.items()
        }
        self.placements: dict[str, Placement] = {}
        # Tasks of the same recipient whose windows could overlap must get a
        # fixed time so that recipient non-overlap can be tracked exactly.
        self.fixed_time_tasks: set[str] = set()
        by_r: dict[str, list[Task]] = {}
        for t in problem.tasks:
            by_r.setdefault(t.recipient_id, []).append(t)
        for rid, ts in by_r.items():
            busy = problem.recipient_busy.get(rid, [])
            for a in ts:
                if any(s < a.latest + a.duration and a.earliest < e for s, e in busy):
                    self.fixed_time_tasks.add(a.visit_id)
                for b in ts:
                    if a is not b and a.earliest < b.latest + b.duration and b.earliest < a.latest + a.duration:
                        self.fixed_time_tasks.add(a.visit_id)

    # ------------------------------------------------------------------
    def _options(self, task: Task, role: int) -> list[tuple[InsertionOption, int]]:
        stop = Stop("visit", task.visit_id, role, task.location_id, task.duration, task.earliest, task.latest)
        out = []
        for eid in task.allowed[role]:
            route = self.routes.get(eid)
            if route is None:
                continue
            v = route.vehicle
            if v.start_time > task.latest or v.end_time < task.earliest + task.duration:
                continue
            if task.visit_id in route_visit_ids(route, self.tm):
                continue
            for opt in insertion_options(route, stop, self.tm):
                pen = assignment_penalty(self.p, task, eid) + self.p.weights.travel_minute * opt.delta_travel
                out.append((opt, pen))
        return out

    def _insert(self, task: Task, role: int, opt: InsertionOption, start: int, fixed: bool) -> None:
        lo, hi = (start, start) if fixed else (task.earliest, task.latest)
        stop = Stop("visit", task.visit_id, role, task.location_id, task.duration, lo, hi)
        self.routes[opt.employee_id].stops.insert(opt.position, stop)

    def try_insert(self, task: Task) -> bool:
        if task.visit_id in self.placements:
            return True
        busy = self.recipient_busy.setdefault(task.recipient_id, [])
        if task.pinned:
            return self._insert_pinned(task, busy)
        tol = self.p.settings.sync_tolerance_min
        target = task.previous_start if task.previous_start is not None else task.preferred
        if task.roles == 1:
            best = None
            for opt, pen in self._options(task, 0):
                t = _pick_time(opt.lo, opt.hi, target, task.duration, busy)
                if t is None:
                    continue
                cost = pen + time_deviation_cost(self.p, task, t)
                if best is None or cost < best[0]:
                    best = (cost, opt, t)
            if best is None:
                return False
            _, opt, t = best
            fixed = task.visit_id in self.fixed_time_tasks
            self._insert(task, 0, opt, t, fixed)
            self.placements[task.visit_id] = Placement(task, {0: (opt.employee_id, t)})
            if fixed:
                busy.append((t, t + task.duration))
            return True

        lead = self._options(task, 0)
        if not lead:
            return False
        assist = self._options(task, 1)
        best = None
        for o1, p1 in lead:
            for o2, p2 in assist:
                if o1.employee_id == o2.employee_id:
                    continue
                lo = max(o1.lo, o2.lo)
                hi = min(o1.hi, o2.hi)
                if lo > hi + tol:
                    continue
                t = _pick_time(lo, max(lo, hi), target, task.duration, busy) if lo <= hi else None
                if t is None:
                    continue
                cost = p1 + p2 + time_deviation_cost(self.p, task, t)
                if best is None or cost < best[0]:
                    best = (cost, o1, o2, t)
        if best is None:
            return False
        _, o1, o2, t = best
        self._insert(task, 0, o1, t, True)
        self._insert(task, 1, o2, t, True)
        self.placements[task.visit_id] = Placement(task, {0: (o1.employee_id, t), 1: (o2.employee_id, t)})
        busy.append((t, t + task.duration))
        return True

    def _insert_pinned(self, task: Task, busy: list[tuple[int, int]]) -> bool:
        assert task.pinned is not None
        chosen: dict[int, tuple[str, int]] = {}
        for role, (eid, start) in sorted(task.pinned.items()):
            route = self.routes.get(eid)
            if route is None:
                return False
            stop = Stop("visit", task.visit_id, role, task.location_id, task.duration, start, start)
            opts = [o for o in insertion_options(route, stop, self.tm) if o.lo <= start <= o.hi]
            if not opts:
                return False
            opt = min(opts, key=lambda o: o.delta_travel)
            route.stops.insert(opt.position, stop)
            chosen[role] = (eid, start)
        self.placements[task.visit_id] = Placement(task, chosen)
        busy.append((min(s for _, s in chosen.values()), max(s for _, s in chosen.values()) + task.duration))
        return True

    def run(self, tasks: list[Task] | None = None) -> list[Task]:
        """Insert tasks; returns those that could not be inserted."""
        failed = []
        for task in sorted(tasks if tasks is not None else self.p.tasks, key=task_order_key):
            if not self.try_insert(task):
                failed.append(task)
        return failed

    def hint_routes(self) -> dict[str, list[tuple[str, int]]]:
        """employee -> [(visit_id, role)]; breaks appear as ("#BREAK", k)."""
        return {
            eid: [(s.visit_id or "#BREAK", s.role) for s in r.stops]
            for eid, r in self.routes.items()
        }
