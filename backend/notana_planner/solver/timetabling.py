"""CP-SAT timetabling: exact start times for fixed visit sequences.

Routing decides *who* does *what* in *which order*. Given those sequences,
this CP-SAT model chooses exact start times for every visit and break:

* hard: windows, travel + full duration between consecutive stops, shift
  start / end, double-staffing synchronisation across routes (|s_a - s_b| <=
  tolerance), recipient non-overlap, pinned / locked starts
* soft (minimised): deviation from preferred start, deviation from previously
  communicated start (re-planning), overtime, idle time (route span)

Because double-staffed visits link two routes, this is a genuinely global
model; CP-SAT solves it to optimality in well under a second for a full day.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from ..travel import TravelMatrix
from .problem import PlanningProblem, Task, VehicleSpec


@dataclass(slots=True)
class TTStop:
    kind: str  # "visit" | "break"
    visit_id: str | None
    role: int
    location: str
    duration: int
    earliest: int
    latest: int
    fixed: int | None = None
    free_arrival: bool = False


@dataclass(slots=True)
class TTRoute:
    vehicle: VehicleSpec
    stops: list[TTStop]


@dataclass(slots=True)
class TTResult:
    ok: bool
    status: str
    starts: dict[str, list[int]]  # employee -> start per stop
    route_start: dict[str, int]
    route_end: dict[str, int]
    objective: float | None
    wall_time_s: float
    notes: list[str] = field(default_factory=list)


def timetable(
    problem: PlanningProblem,
    routes: dict[str, TTRoute],
    tasks: dict[str, Task],
    time_limit_s: float = 10.0,
) -> TTResult:
    t0 = time.perf_counter()
    tm: TravelMatrix = problem.travel
    w = problem.weights
    tol = problem.settings.sync_tolerance_min
    m = cp_model.CpModel()
    svars: dict[str, list[cp_model.IntVar]] = {}
    rs: dict[str, cp_model.IntVar] = {}
    re_: dict[str, cp_model.IntVar] = {}
    obj = []
    occurrences: dict[str, list[tuple[str, int]]] = {}

    for eid, route in routes.items():
        v = route.vehicle
        lo, hi = v.start_time, max(v.start_time, v.end_time)
        r_start = m.NewIntVar(lo, hi, f"rs_{eid}")
        r_end = m.NewIntVar(lo, hi, f"re_{eid}")
        rs[eid], re_[eid] = r_start, r_end
        vs = []
        prev_end = r_start
        prev_loc = v.start_location
        for k, st in enumerate(route.stops):
            if st.fixed is not None:
                x = m.NewConstant(st.fixed)
            else:
                x = m.NewIntVar(st.earliest, st.latest, f"s_{eid}_{k}")
            vs.append(x)
            m.Add(x >= prev_end + (0 if st.free_arrival else tm.minutes(prev_loc, st.location)))
            prev_end = x + st.duration
            prev_loc = st.location
            if st.kind == "visit" and st.visit_id is not None:
                occurrences.setdefault(st.visit_id, []).append((eid, k))
                task = tasks.get(st.visit_id)
                if task is not None and st.fixed is None and st.role == 0:
                    # Deviation costs are charged once per visit (on the lead role).
                    if w.preferred_time_minute:
                        d = m.NewIntVar(0, 24 * 60, f"dp_{eid}_{k}")
                        m.AddAbsEquality(d, x - task.preferred)
                        obj.append(w.preferred_time_minute * d)
                    if task.previous_start is not None and w.time_change_minute:
                        d2 = m.NewIntVar(0, 24 * 60, f"dc_{eid}_{k}")
                        m.AddAbsEquality(d2, x - task.previous_start)
                        obj.append(w.time_change_minute * d2)
        m.Add(r_end >= prev_end + tm.minutes(prev_loc, v.end_location))
        if w.idle_minute:
            obj.append(w.idle_minute * (r_end - r_start))
        if v.end_time > v.shift_end and w.overtime_minute:
            ot = m.NewIntVar(0, v.end_time - v.shift_end, f"ot_{eid}")
            m.Add(ot >= r_end - v.shift_end)
            obj.append(w.overtime_minute * ot)
        svars[eid] = vs

    # Double-staffing synchronisation.
    for vid, occ in occurrences.items():
        for (e1, k1), (e2, k2) in zip(occ, occ[1:]):
            a, b = svars[e1][k1], svars[e2][k2]
            m.Add(a - b <= tol)
            m.Add(b - a <= tol)

    # Recipient non-overlap between visits of the same recipient (incl. fixed busy).
    by_r: dict[str, list[tuple[str, cp_model.IntVar, int]]] = {}
    for vid, occ in occurrences.items():
        task = tasks.get(vid)
        if task is None:
            continue
        e, k = occ[0]
        by_r.setdefault(task.recipient_id, []).append((vid, svars[e][k], task.duration + tol))
    pairs = 0
    for rid, items in by_r.items():
        busy = problem.recipient_busy.get(rid, [])
        for i, (va, xa, da) in enumerate(items):
            for s0, e0 in busy:
                b = m.NewBoolVar("")
                m.Add(xa + da <= s0).OnlyEnforceIf(b)
                m.Add(xa >= e0).OnlyEnforceIf(b.Not())
                pairs += 1
            for vb, xb, db in items[i + 1 :]:
                b = m.NewBoolVar("")
                m.Add(xa + da <= xb).OnlyEnforceIf(b)
                m.Add(xb + db <= xa).OnlyEnforceIf(b.Not())
                pairs += 1

    if obj:
        m.Minimize(sum(obj))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 4
    status = solver.Solve(m)
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return TTResult(False, name, {}, {}, {}, None, time.perf_counter() - t0, [f"{pairs} recipient pairs"])
    return TTResult(
        ok=True,
        status=name,
        starts={e: [solver.Value(x) for x in vs] for e, vs in svars.items()},
        route_start={e: solver.Value(x) for e, x in rs.items()},
        route_end={e: solver.Value(x) for e, x in re_.items()},
        objective=solver.ObjectiveValue() if obj else 0.0,
        wall_time_s=time.perf_counter() - t0,
        notes=[f"{pairs} recipient non-overlap pairs"],
    )
