"""Solve pipeline for one :class:`PlanningProblem`.

1. **Construction** -- double-staffed visits are inserted as synchronised
   pairs (two different qualified employees, same start) by the exact
   insertion heuristic, then single visits. Warm-start routes (re-planning)
   are reused first.
2. **OR-Tools routing** -- guided local search over assignment and sequence
   of all single-staffed visits around the (pinned) pairs, breaks and locked
   visits, warm-started from step 1. All hard constraints are model
   constraints; dropped visits can be re-inserted by the search.
3. **Repair insertion** -- leftover visits (incl. pairs) are re-tried with
   exact feasibility checks against the routed solution.
4. **Enhanced repair** (optional strategy) -- pair relocation and
   ruin-and-recreate over the leftovers.
5. **CP-SAT timetabling** -- exact start times for the fixed sequences,
   global double-staffing synchronisation and soft time preferences.

The result is *not* trusted: the plan is validated independently afterwards.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field

from .construction import Constructor, Placement, initial_routes
from .problem import PlanningProblem, Task
from .routes import Stop, WorkRoute, forward
from .routing_solver import RoutingSolver
from .timetabling import TTRoute, TTStop, timetable


@dataclass(slots=True)
class SolvedStop:
    kind: str
    visit_id: str | None
    role: int
    location: str
    duration: int
    start: int
    free_arrival: bool = False


@dataclass(slots=True)
class SolveOutput:
    routes: dict[str, list[SolvedStop]]
    route_start: dict[str, int]
    route_end: dict[str, int]
    unplanned: set[str]
    stats: dict = field(default_factory=dict)


@dataclass(slots=True)
class SolveOptions:
    time_limit_s: float = 25.0
    repair_insertion: bool = True
    enhanced_repair: bool = False
    enhanced_rounds: int = 2
    # Warm-start routing from a full insertion construction (True) or let
    # OR-Tools build the first solution itself (False, LOCAL_CHEAPEST_INSERTION).
    construct_singles: bool = True


def _stop_for(task: Task, role: int, fixed: int | None) -> Stop:
    lo, hi = (fixed, fixed) if fixed is not None else (task.earliest, task.latest)
    return Stop("visit", task.visit_id, role, task.location_id, task.duration, lo, hi)


def _routes_from_hint(
    problem: PlanningProblem, tasks: dict[str, Task]
) -> tuple[dict[str, WorkRoute], dict[str, dict[int, tuple[str, int]]], list[Task]]:
    """Warm-start work routes from the hint.

    Keeps, per employee, the hinted visits in time order as long as the route
    stays feasible (e.g. after a delay or a traffic change, infeasible visits
    are released). Double-staffed visits are kept only if both roles survive
    at the same start. Returns (routes, placements, tasks to (re)insert).
    """
    routes = initial_routes(problem)
    hint = problem.hint_routes or {}
    placed: dict[str, dict[int, tuple[str, int]]] = {}
    for eid, seq in hint.items():
        r = routes.get(eid)
        if r is None:
            continue
        cand_stops: list[tuple[int, Stop]] = [(s.earliest, s) for s in r.stops]
        for vid, role, start in seq:
            t = tasks.get(vid)
            if t is None or role >= t.roles:
                continue
            if t.pinned and role in t.pinned:
                if t.pinned[role][0] != eid:
                    continue
                fixed: int | None = t.pinned[role][1]
            else:
                if eid not in t.allowed[role]:
                    continue
                fixed = start if t.roles == 2 else None
            cand_stops.append((start, _stop_for(t, role, fixed)))
        cand_stops.sort(key=lambda x: (x[0], x[1].kind != "break"))
        cur: list[Stop] = []
        for start, st in cand_stops:
            trial = cur + [st]
            if st.kind == "break" or forward(trial, r.vehicle, problem.travel) is not None:
                cur = trial
                if st.kind == "visit":
                    placed.setdefault(st.visit_id, {})[st.role] = (eid, start)  # type: ignore[index]
        r.stops = cur
    reinsert: list[Task] = []
    for vid, t in tasks.items():
        roles = placed.get(vid, {})
        complete = len(roles) == t.roles and len({s for _, s in roles.values()}) == 1
        if not complete:
            for eid, _ in roles.values():
                routes[eid].stops = [s for s in routes[eid].stops if s.visit_id != vid]
            placed.pop(vid, None)
            reinsert.append(t)
    # Releasing a partial pair may leave a route infeasible only if a break
    # depended on it -- impossible, removal never delays later stops.
    return routes, placed, reinsert


class SolveEngine:
    def __init__(self, problem: PlanningProblem, options: SolveOptions | None = None) -> None:
        self.p = problem
        self.opt = options or SolveOptions()

    def run(self) -> SolveOutput:
        t0 = time.perf_counter()
        p = self.p
        stats: dict = {"label": p.label, "vehicles": len(p.vehicles), "tasks": len(p.tasks)}
        tasks = {t.visit_id: t for t in p.tasks}

        # ---- 1. pair construction (and warm start) -------------------------
        if p.hint_routes:
            routes, placed0, to_insert = _routes_from_hint(p, tasks)
            ctor = Constructor(p, routes)
            for vid, roles in placed0.items():
                ctor.placements[vid] = Placement(tasks[vid], roles)
        else:
            ctor = Constructor(p)
            to_insert = list(p.tasks)
        pinned_or_double = [t for t in to_insert if t.roles == 2 or t.pinned]
        failed_pairs = ctor.run(pinned_or_double)
        singles = [t for t in to_insert if not (t.roles == 2 or t.pinned)]
        failed_singles = ctor.run(singles) if self.opt.construct_singles else singles
        stats["construction"] = {
            "pairs_inserted": len(pinned_or_double) - len(failed_pairs),
            "pairs_failed": len(failed_pairs),
            "singles_inserted": len(singles) - len(failed_singles),
            "singles_failed": len(failed_singles),
            "seconds": round(time.perf_counter() - t0, 2),
        }

        # ---- 2. OR-Tools routing -------------------------------------------
        rp = copy.copy(p)
        rp_tasks = []
        for t in p.tasks:
            pl = ctor.placements.get(t.visit_id)
            if t.roles == 2 or t.pinned:
                if pl is None:
                    continue  # pair not constructible -> handled by repair stage
                t = copy.copy(t)
                t.pinned = dict(pl.roles)
            rp_tasks.append(t)
        rp.tasks = rp_tasks
        hint_seq = ctor.hint_routes() if (p.hint_routes or self.opt.construct_singles) else None
        remaining = self.opt.time_limit_s - (time.perf_counter() - t0)
        if self.opt.enhanced_repair:
            remaining *= 0.8
        rr = RoutingSolver(rp).solve(max(1.0, remaining), hint=hint_seq)
        stats["routing"] = {
            "status": rr.status,
            "objective": rr.objective,
            "seconds": round(rr.wall_time_s, 2),
            "warm_start": rr.used_hint,
            "dropped": len(rr.dropped),
            **rr.stats,
        }

        # ---- 3. rebuild exact work routes from the routing sequences --------
        work = initial_routes(p)
        placed: dict[str, dict[int, tuple[str, int]]] = {}
        for eid, seq in rr.routes.items():
            r = work[eid]
            stops: list[Stop] = []
            breaks = list(r.stops)
            for vid, role, cmin, _ in seq:
                if vid == "#BREAK":
                    stops.append(breaks[role])
                    continue
                t = tasks[vid]
                fixed = None
                if t.pinned and role in t.pinned:
                    fixed = t.pinned[role][1]
                elif t.roles == 2:
                    fixed = cmin  # pinned at its constructed start during routing
                stops.append(_stop_for(t, role, fixed))
                placed.setdefault(vid, {})[role] = (eid, cmin)
            r.stops = stops
        if rr.status in ("ROUTING_FAIL", "ROUTING_FAIL_TIMEOUT", "ROUTING_INVALID", "ROUTING_INFEASIBLE") and not any(
            rr.routes.values()
        ):
            # Routing produced nothing usable: fall back to the construction routes.
            stats["routing"]["fallback"] = "construction"
            work = ctor.routes
            rest = [t for t in p.tasks if t.visit_id not in ctor.placements]
            ctor.run(rest)
            placed = {k: dict(v.roles) for k, v in ctor.placements.items()}

        # Breaks / unavailable gaps are hard: if routing dropped one (it only does so
        # when it cannot fit), put it back and release visits until the route is feasible.
        released = _ensure_breaks(p, work, tasks, placed)
        if released:
            stats["break_guard"] = {"released_visits": sorted(released)}
        unplanned = {vid for vid in tasks if vid not in placed or len(placed[vid]) < tasks[vid].roles}
        repair = Constructor(p, work)
        for vid, roles in placed.items():
            repair.placements[vid] = Placement(tasks[vid], roles)
        if self.opt.repair_insertion and unplanned:
            before = len(unplanned)
            left = repair.run([tasks[v] for v in unplanned])
            unplanned = {t.visit_id for t in left}
            stats["repair_insertion"] = {"tried": before, "inserted": before - len(unplanned)}

        # ---- 4. enhanced repair ---------------------------------------------
        if self.opt.enhanced_repair and unplanned:
            from .lns import enhanced_repair

            unplanned, lns_stats = enhanced_repair(p, repair, unplanned, rounds=self.opt.enhanced_rounds)
            stats["enhanced_repair"] = lns_stats

        # ---- 5. CP-SAT timetabling ------------------------------------------
        tt_routes: dict[str, TTRoute] = {}
        for eid, r in repair.routes.items():
            tts = []
            for st in r.stops:
                if st.kind == "break":
                    tts.append(TTStop("break", None, st.role, st.location, st.duration, st.earliest, st.latest,
                                      free_arrival=st.free_arrival))
                    continue
                t = tasks[st.visit_id]  # type: ignore[index]
                fixed = t.pinned[st.role][1] if t.pinned and st.role in t.pinned else None
                tts.append(TTStop("visit", t.visit_id, st.role, t.location_id, t.duration, t.earliest, t.latest, fixed))
            tt_routes[eid] = TTRoute(r.vehicle, tts)
        tt = timetable(p, tt_routes, tasks) if p.settings.use_cpsat_timetabling else None
        out_routes: dict[str, list[SolvedStop]] = {}
        r_start: dict[str, int] = {}
        r_end: dict[str, int] = {}
        if tt is not None and tt.ok:
            for eid, route in tt_routes.items():
                out_routes[eid] = [
                    SolvedStop(s.kind, s.visit_id, s.role, s.location, s.duration, start, s.free_arrival)
                    for s, start in zip(route.stops, tt.starts[eid])
                ]
                r_start[eid] = tt.route_start[eid]
                r_end[eid] = tt.route_end[eid]
            stats["timetabling"] = {
                "status": tt.status,
                "seconds": round(tt.wall_time_s, 2),
                "objective": tt.objective,
                "notes": tt.notes,
            }
        else:
            # Fallback: earliest feasible starts from the insertion routes (doubles
            # are fixed there, so synchronisation still holds).
            stats["timetabling"] = {"status": tt.status if tt else "disabled", "fallback": "earliest-start"}
            for eid, r in repair.routes.items():
                starts = forward(r.stops, r.vehicle, p.travel) or [s.earliest for s in r.stops]
                out_routes[eid] = [
                    SolvedStop(s.kind, s.visit_id, s.role, s.location, s.duration, st, s.free_arrival)
                    for s, st in zip(r.stops, starts)
                ]
                r_start[eid] = r.vehicle.start_time
                last_loc = r.stops[-1].location if r.stops else r.vehicle.start_location
                last_end = (starts[-1] + r.stops[-1].duration) if r.stops else r.vehicle.start_time
                r_end[eid] = last_end + (r.vehicle.travel or p.travel).minutes(last_loc, r.vehicle.end_location)
        # ---- 6. fixed-time gap fill -----------------------------------------
        if unplanned and self.opt.repair_insertion:
            filled = _gap_fill(p, tasks, out_routes, r_start, r_end, unplanned)
            unplanned -= filled
            stats["gap_fill"] = {"inserted": len(filled)}

        stats["seconds_total"] = round(time.perf_counter() - t0, 2)
        stats["unplanned"] = len(unplanned)
        return SolveOutput(out_routes, r_start, r_end, unplanned, stats)


def _ensure_breaks(
    p: PlanningProblem,
    work: dict[str, WorkRoute],
    tasks: dict[str, Task],
    placed: dict[str, dict[int, tuple[str, int]]],
) -> set[str]:
    from .routes import insertion_options

    released: set[str] = set()
    templates = initial_routes(p)
    for eid, r in work.items():
        have = {s.role for s in r.stops if s.kind == "break"}
        for brk in templates[eid].stops:
            if brk.role in have:
                continue
            while True:
                opts = insertion_options(r, brk, p.travel)
                if opts:
                    best = min(opts, key=lambda o: o.delta_travel)
                    r.stops.insert(best.position, brk)
                    break
                # Release the free visit closest to the break window.
                cands = [
                    s for s in r.stops
                    if s.kind == "visit" and not (tasks[s.visit_id].pinned)  # type: ignore[index]
                ]
                if not cands:
                    break
                victim = min(cands, key=lambda s: abs(s.earliest - brk.earliest))
                vid = victim.visit_id
                for e2, rr in work.items():
                    rr.stops = [s for s in rr.stops if s.visit_id != vid]
                placed.pop(vid, None)  # type: ignore[arg-type]
                released.add(vid)  # type: ignore[arg-type]
    return released


def _free_intervals(
    p: PlanningProblem, task: Task, eid: str, stops: list[SolvedStop], r_start: int | None, r_end: int | None
) -> list[tuple[int, int, int]]:
    """(lo, hi, position) start intervals for ``task`` between fixed stops."""
    v = next(x for x in p.vehicles if x.employee_id == eid)
    tm = v.travel or p.travel
    out = []
    prev_loc, prev_end = v.start_location, v.start_time
    seq = stops + [None]
    for pos, nxt in enumerate(seq):
        if nxt is None:
            nxt_loc, nxt_start = v.end_location, v.end_time
        else:
            nxt_loc, nxt_start = nxt.location, nxt.start
        lo = max(task.earliest, prev_end + tm.minutes(prev_loc, task.location_id))
        to_next = 0 if (nxt is not None and nxt.free_arrival) else tm.minutes(task.location_id, nxt_loc)
        hi = min(task.latest, nxt_start - task.duration - to_next)
        if lo <= hi:
            out.append((lo, hi, pos))
        if nxt is not None:
            prev_loc, prev_end = nxt.location, nxt.start + nxt.duration
    return out


def _gap_fill(
    p: PlanningProblem,
    tasks: dict[str, Task],
    routes: dict[str, list[SolvedStop]],
    r_start: dict[str, int],
    r_end: dict[str, int],
    unplanned: set[str],
) -> set[str]:
    """Insert leftovers into openings created by timetabling, moving nothing."""
    filled: set[str] = set()
    busy: dict[str, list[tuple[int, int]]] = {k: list(v) for k, v in p.recipient_busy.items()}
    for eid, stops in routes.items():
        for st in stops:
            if st.kind == "visit" and st.visit_id in tasks:
                busy.setdefault(tasks[st.visit_id].recipient_id, []).append((st.start, st.start + st.duration))
    tol = p.settings.sync_tolerance_min
    for vid in sorted(unplanned, key=lambda x: (-tasks[x].priority, tasks[x].earliest)):
        t = tasks[vid]
        if t.pinned:
            continue
        rb = busy.get(t.recipient_id, [])

        def ok_time(s: int) -> bool:
            return all(not (s < e0 + tol and s0 < s + t.duration + tol) for s0, e0 in rb)

        options = {}
        for role in range(t.roles):
            options[role] = [
                (eid, lo, hi, pos)
                for eid in t.allowed[role]
                if eid in routes
                for lo, hi, pos in _free_intervals(p, t, eid, routes[eid], r_start.get(eid), r_end.get(eid))
            ]
        chosen: list[tuple[int, str, int, int]] | None = None
        if t.roles == 1:
            best = None
            for eid, lo, hi, pos in options[0]:
                s = min(max(t.preferred, lo), hi)
                if not ok_time(s):
                    s = next((x for x in range(lo, hi + 1) if ok_time(x)), None)  # type: ignore[assignment]
                    if s is None:
                        continue
                if best is None or abs(s - t.preferred) < abs(best[2] - t.preferred):
                    best = (0, eid, s, pos)
            chosen = [best] if best else None
        else:
            best2 = None
            for e1, lo1, hi1, pos1 in options[0]:
                for e2, lo2, hi2, pos2 in options[1]:
                    if e1 == e2:
                        continue
                    lo, hi = max(lo1, lo2), min(hi1, hi2)
                    if lo > hi:
                        continue
                    s = min(max(t.preferred, lo), hi)
                    if not ok_time(s):
                        continue
                    if best2 is None or abs(s - t.preferred) < abs(best2[0] - t.preferred):
                        best2 = (s, e1, pos1, e2, pos2)
            if best2:
                s, e1, pos1, e2, pos2 = best2
                chosen = [(0, e1, s, pos1), (1, e2, s, pos2)]
        if not chosen:
            continue
        for role, eid, s, pos in chosen:
            v = next(x for x in p.vehicles if x.employee_id == eid)
            stops = routes[eid]
            stops.insert(pos, SolvedStop("visit", vid, role, t.location_id, t.duration, s))
            # Keep departure / return consistent with the new first / last stop.
            vtm = v.travel or p.travel
            depart = s - vtm.minutes(v.start_location, t.location_id)
            if pos == 0:
                r_start[eid] = min(r_start.get(eid, depart), depart)
            if pos == len(stops) - 1:
                ret = s + t.duration + vtm.minutes(t.location_id, v.end_location)
                r_end[eid] = max(r_end.get(eid, ret), ret)
        busy.setdefault(t.recipient_id, []).append((chosen[0][2], chosen[0][2] + t.duration))
        filled.add(vid)
    return filled
