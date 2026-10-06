"""OR-Tools vehicle-routing model: assignment + sequencing.

Every employee is a vehicle with its own start/end node. Every role of every
task is a node. Hard constraints are model constraints, not penalties:

* shift: start/end cumul ranges of each vehicle
* time windows: cumul range of each visit node (start-of-service window)
* travel + full duration: transit(i, j) = duration(i) + travel(i, j)
* breaks: mandatory break nodes at the team office, own start window
* qualifications: allowed vehicles per node (lead: skills + delegations)
* double staffing: both roles active together, equal start (+- tolerance),
  on different vehicles
* recipient non-overlap for visits of one recipient whose windows intersect
* pinned / locked: fixed vehicle, fixed start, mandatory

Soft objectives: priority-weighted drop penalties (disjunctions), travel time
and distance (arc costs), preferred / communicated start (soft cumul bounds),
continuity + stability (per-vehicle unary penalty dimension), workload balance
(soft upper bound on per-vehicle care minutes), overtime and idle time.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .costs import assignment_penalty, time_target
from .problem import PlanningProblem, Task

HORIZON = 2 * 24 * 60
# Pinned visits and breaks get disjunctions with penalties far above any other
# cost. Without a disjunction LOCAL_CHEAPEST_INSERTION inserts them *last*
# (it orders nodes by penalty) and then fails on fragmented routes. With these
# penalties they are inserted first and are only ever dropped if impossible,
# in which case the plan reports the conflict / the validator flags it.
PINNED_PENALTY = 10**11
BREAK_PENALTY = 10**12
# Assignment costs as C++ unary vectors (fast) instead of Python callbacks.
UNARY_ASSIGN = os.environ.get("NOTANA_ASSIGN_CALLBACK", "unary") != "python"
_DEBUG = bool(os.environ.get("NOTANA_DEBUG"))


def _dbg(msg: str, t0: float) -> None:
    if _DEBUG:
        print(f"[routing {time.perf_counter() - t0:7.2f}s] {msg}", flush=True)

_STATUS = {
    0: "ROUTING_NOT_SOLVED",
    1: "ROUTING_SUCCESS",
    2: "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED",
    3: "ROUTING_FAIL",
    4: "ROUTING_FAIL_TIMEOUT",
    5: "ROUTING_INVALID",
    6: "ROUTING_INFEASIBLE",
    7: "ROUTING_OPTIMAL",
}


@dataclass(slots=True)
class RoutingResult:
    # employee -> ordered [(visit_id, role, cumul_min, cumul_max)]
    routes: dict[str, list[tuple[str, int, int, int]]]
    dropped: set[str]
    status: str
    objective: int | None
    wall_time_s: float
    used_hint: bool
    stats: dict = field(default_factory=dict)


class RoutingSolver:
    def __init__(self, problem: PlanningProblem) -> None:
        self.p = problem

    def solve(self, time_limit_s: float, hint: dict[str, list[tuple[str, int]]] | None = None) -> RoutingResult:
        t0 = time.perf_counter()
        p = self.p
        w = p.weights
        tm = p.travel
        V = len(p.vehicles)
        # Tasks that no employee in this problem may perform are dropped up front.
        pre_dropped = {t.visit_id for t in p.tasks if not t.pinned and any(not a for a in t.allowed)}
        tasks = [t for t in p.tasks if t.visit_id not in pre_dropped]
        node_task: list[Task | None] = [None] * (2 * V)
        node_role: list[int] = [0] * (2 * V)
        node_loc: list[str] = [v.start_location for v in p.vehicles] + [v.end_location for v in p.vehicles]
        node_svc: list[int] = [0] * (2 * V)
        task_nodes: dict[str, list[int]] = {}
        for t in tasks:
            ids = []
            for r in range(t.roles):
                ids.append(len(node_task))
                node_task.append(t)
                node_role.append(r)
                node_loc.append(t.location_id)
                node_svc.append(t.duration)
            task_nodes[t.visit_id] = ids
        # Breaks are mandatory nodes pinned to their employee (taken at the team office).
        break_nodes: dict[tuple[int, int], int] = {}
        node_break: dict[int, tuple[int, int]] = {}
        for vi, v in enumerate(p.vehicles):
            for k, b in enumerate(v.breaks):
                n = len(node_task)
                node_task.append(None)
                node_role.append(k)
                node_loc.append(v.break_location or v.start_location)
                node_svc.append(b.duration_minutes)
                break_nodes[(vi, k)] = n
                node_break[n] = (vi, k)
        N = len(node_task)
        free_in = {n for (vi, k), n in break_nodes.items() if p.vehicles[vi].breaks[k].kind == "unavailable"}

        if not tasks:
            return RoutingResult({v.employee_id: [] for v in p.vehicles}, pre_dropped, "EMPTY", 0, 0.0, False)

        manager = pywrapcp.RoutingIndexManager(N, V, list(range(V)), list(range(V, 2 * V)))
        routing = pywrapcp.RoutingModel(manager)
        solver = routing.solver()

        # One time / cost matrix per travel mode (car, bike ...): vehicles that share
        # a TravelMatrix share the registered callbacks.
        groups: dict[int, tuple[object, list[int]]] = {}
        for vi, v in enumerate(p.vehicles):
            vtm = v.travel or tm
            groups.setdefault(id(vtm), (vtm, []))[1].append(vi)
        li = [tm.index[loc] for loc in node_loc]
        km_rows = tm.base.km
        time_cb_of: dict[int, int] = {}
        for gtm, members in groups.values():
            rows = gtm.rows()  # type: ignore[attr-defined]
            time_m: list[list[int]] = []
            cost_m: list[list[int]] = []
            for i in range(N):
                ri = rows[li[i]]
                kri = km_rows[li[i]]
                svc = node_svc[i]
                if V <= i < 2 * V:  # end nodes have no outgoing arcs
                    time_m.append([0] * N)
                    cost_m.append([0] * N)
                    continue
                trow = [0 if j in free_in else ri[li[j]] for j in range(N)]
                time_m.append([svc + x for x in trow])
                thr = w.long_leg_threshold_min
                cost_m.append([
                    w.travel_minute * trow[j] + int(w.travel_km * kri[li[j]]) + w.long_leg_minute * max(0, trow[j] - thr)
                    for j in range(N)
                ])
            time_cb = routing.RegisterTransitMatrix(time_m)
            cost_cb = routing.RegisterTransitMatrix(cost_m)
            for vi in members:
                time_cb_of[vi] = time_cb
                routing.SetArcCostEvaluatorOfVehicle(cost_cb, vi)
        _dbg(f"matrices built ({len(groups)} travel modes)", t0)
        if len(groups) == 1:
            routing.AddDimension(time_cb_of[0], HORIZON, HORIZON, False, "Time")
        else:
            routing.AddDimensionWithVehicleTransits([time_cb_of[vi] for vi in range(V)], HORIZON, HORIZON, False, "Time")
        tdim = routing.GetDimensionOrDie("Time")
        tdim.SetSpanCostCoefficientForAllVehicles(w.idle_minute)

        for vi, v in enumerate(p.vehicles):
            s_idx, e_idx = routing.Start(vi), routing.End(vi)
            tdim.CumulVar(s_idx).SetRange(v.start_time, max(v.start_time, v.end_time))
            tdim.CumulVar(e_idx).SetRange(v.start_time, max(v.start_time, v.end_time))
            if v.end_time > v.shift_end and w.overtime_minute:
                tdim.SetCumulVarSoftUpperBound(e_idx, v.shift_end, w.overtime_minute)

        _dbg("time dim", t0)
        for (vi, k), n in break_nodes.items():
            b = p.vehicles[vi].breaks[k]
            idx = manager.NodeToIndex(n)
            tdim.CumulVar(idx).SetRange(b.earliest_start, b.latest_start)
            routing.VehicleVar(idx).SetValues([vi, -1])
            routing.AddDisjunction([idx], BREAK_PENALTY)

        _dbg("breaks", t0)
        vidx = p.vehicle_index()

        # Continuity / stability: per-vehicle penalties of serving a node, summed
        # via the span cost of a vehicle-dependent dimension. Registered as
        # *binary* callbacks: the unary-vector variant of vehicle-dependent
        # transits makes valid models infeasible in some OR-Tools builds.
        pen_cbs = []
        max_pen = 1
        for vi, v in enumerate(p.vehicles):
            vec = [0] * N
            for n in range(2 * V, N):
                t = node_task[n]
                if t is not None and not t.pinned:  # pinned: vehicle fixed, cost constant
                    vec[n] = assignment_penalty(p, t, v.employee_id)
            max_pen = max(max_pen, sum(vec))
            if UNARY_ASSIGN:
                pen_cbs.append(routing.RegisterUnaryTransitVector(vec))
            else:
                pen_cbs.append(
                    routing.RegisterTransitCallback(lambda i, j, vec=vec: vec[manager.IndexToNode(i)])
                )
        routing.AddDimensionWithVehicleTransits(pen_cbs, 0, max_pen + 1, True, "Assign")
        routing.GetDimensionOrDie("Assign").SetSpanCostCoefficientForAllVehicles(1)

        _dbg("assign dim", t0)
        # Workload balance: care minutes above fair share are penalised.
        work_vec = [svc if node_task[n] is not None else 0 for n, svc in enumerate(node_svc)]
        work_cb = routing.RegisterUnaryTransitVector(work_vec)
        total_svc = sum(work_vec)
        routing.AddDimension(work_cb, 0, total_svc + 1, True, "Work")
        wdim = routing.GetDimensionOrDie("Work")
        for vi, v in enumerate(p.vehicles):
            e_idx = routing.End(vi)
            if w.workload_overload_minute:
                wdim.SetCumulVarSoftUpperBound(e_idx, v.fair_workload, w.workload_overload_minute)
            if v.max_workload is not None:
                wdim.CumulVar(e_idx).SetMax(v.max_workload)

        _dbg("work dim", t0)
        # Visit nodes
        tol = p.settings.sync_tolerance_min
        for t in tasks:
            nodes = task_nodes[t.visit_id]
            target = time_target(t)
            coef = w.preferred_time_minute + (w.time_change_minute if t.previous_start is not None else 0)
            for r, n in enumerate(nodes):
                idx = manager.NodeToIndex(n)
                cum = tdim.CumulVar(idx)
                if t.pinned and r in t.pinned:
                    eid, start = t.pinned[r]
                    cum.SetRange(start, start)
                    routing.VehicleVar(idx).SetValues([vidx[eid], -1])
                    routing.AddDisjunction([idx], PINNED_PENALTY)
                    continue
                cum.SetRange(t.earliest, t.latest)
                allowed = [vidx[e] for e in t.allowed[r] if e in vidx]
                # (SetAllowedVehiclesForIndex's Span binding is broken in some
                # OR-Tools wheels; restricting the vehicle var is equivalent.)
                routing.VehicleVar(idx).SetValues(allowed + [-1])
                if coef:
                    tdim.SetCumulVarSoftUpperBound(idx, target, coef)
                    tdim.SetCumulVarSoftLowerBound(idx, target, coef)
                routing.AddDisjunction([idx], t.penalty)
            fully_pinned = bool(t.pinned) and len(t.pinned) == t.roles
            if t.roles == 2 and not fully_pinned:
                # Pinned pairs already have fixed, distinct vehicles and equal fixed
                # starts; cross-route constraints on them only hinder insertion.
                a, b = (manager.NodeToIndex(n) for n in nodes)
                ca, cb = tdim.CumulVar(a), tdim.CumulVar(b)
                solver.Add(routing.ActiveVar(a) == routing.ActiveVar(b))
                solver.Add(ca - cb <= tol)
                solver.Add(cb - ca <= tol)
                solver.Add(abs(routing.VehicleVar(a) - routing.VehicleVar(b)) >= routing.ActiveVar(a))

        _dbg("visit nodes", t0)
        # Recipient non-overlap
        by_r: dict[str, list[Task]] = {}
        for t in tasks:
            by_r.setdefault(t.recipient_id, []).append(t)
        n_pairs = 0
        for rid, ts in by_r.items():
            busy = p.recipient_busy.get(rid, [])
            for i, a in enumerate(ts):
                ia = manager.NodeToIndex(task_nodes[a.visit_id][0])
                ca, aa = tdim.CumulVar(ia), routing.ActiveVar(ia)
                da = a.duration + tol
                for s, e in busy:
                    if s < a.latest + da and a.earliest < e:
                        n_pairs += 1
                        solver.Add(solver.IsLessOrEqualCstVar(ca + da, s) + solver.IsGreaterOrEqualCstVar(ca, e) >= aa)
                for b in ts[i + 1 :]:
                    db = b.duration + tol
                    if a.earliest < b.latest + db and b.earliest < a.latest + da:
                        ib = manager.NodeToIndex(task_nodes[b.visit_id][0])
                        cb, ab = tdim.CumulVar(ib), routing.ActiveVar(ib)
                        n_pairs += 1
                        solver.Add(
                            solver.IsLessOrEqualVar(ca + da, cb) + solver.IsLessOrEqualVar(cb + db, ca)
                            >= solver.Min(aa, ab)
                        )

        _dbg("recipient pairs", t0)
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = getattr(
            routing_enums_pb2.FirstSolutionStrategy, p.settings.first_solution
        )
        params.local_search_metaheuristic = getattr(
            routing_enums_pb2.LocalSearchMetaheuristic, p.settings.metaheuristic
        )
        if p.settings.first_solution == "LOCAL_CHEAPEST_INSERTION":
            # Most important / most constrained nodes first (breaks, pins, then
            # high-priority visits): see PINNED_PENALTY.
            lci = params.local_cheapest_insertion_parameters
            props = type(lci).InsertionSortingProperty
            del lci.insertion_sorting_properties[:]
            lci.insertion_sorting_properties.extend(
                [props.Value("SORTING_PROPERTY_PENALTY"), props.Value("SORTING_PROPERTY_ALLOWED_VEHICLES")]
            )
        build_s = time.perf_counter() - t0
        params.time_limit.FromMilliseconds(int(max(0.5, time_limit_s - build_s) * 1000))
        params.log_search = p.settings.log_search

        _dbg("params", t0)
        used_hint = False
        solution = None
        if hint:
            routes_idx = []
            for v in p.vehicles:
                seq = []
                for vid, role in hint.get(v.employee_id, []):
                    if vid == "#BREAK":
                        bn = break_nodes.get((vidx[v.employee_id], role))
                        if bn is not None:
                            seq.append(manager.NodeToIndex(bn))
                        continue
                    nodes = task_nodes.get(vid)
                    if nodes and role < len(nodes):
                        seq.append(manager.NodeToIndex(nodes[role]))
                routes_idx.append(seq)
            # ReadAssignmentFromRoutes() can hang in some OR-Tools builds;
            # RoutesToAssignment() on a closed model is the robust equivalent.
            routing.CloseModelWithParameters(params)
            initial = routing.solver().Assignment()
            ok = routing.RoutesToAssignment(routes_idx, True, True, initial)
            _dbg(f"hint converted ok={ok}", t0)
            if not ok:
                initial = None
            if initial is not None:
                solution = routing.SolveFromAssignmentWithParameters(initial, params)
                used_hint = solution is not None
        _dbg("hint solve done", t0)
        if solution is None:
            solution = routing.SolveWithParameters(params)

        status = _STATUS.get(routing.status(), str(routing.status()))
        wall = time.perf_counter() - t0
        if solution is None:
            return RoutingResult(
                {v.employee_id: [] for v in p.vehicles},
                {t.visit_id for t in tasks} | pre_dropped,
                status,
                None,
                wall,
                used_hint,
                {"nodes": N, "vehicles": V, "build_s": round(build_s, 2), "recipient_pairs": n_pairs},
            )

        routes: dict[str, list[tuple[str, int, int, int]]] = {}
        active_nodes: set[int] = set()
        for vi, v in enumerate(p.vehicles):
            seq = []
            idx = solution.Value(routing.NextVar(routing.Start(vi)))
            while not routing.IsEnd(idx):
                n = manager.IndexToNode(idx)
                t = node_task[n]
                active_nodes.add(n)
                cum = tdim.CumulVar(idx)
                vid = t.visit_id if t is not None else "#BREAK"
                seq.append((vid, node_role[n], solution.Min(cum), solution.Max(cum)))
                idx = solution.Value(routing.NextVar(idx))
            routes[v.employee_id] = seq
        dropped = pre_dropped | {
            t.visit_id for t in tasks if not all(n in active_nodes for n in task_nodes[t.visit_id])
        }
        return RoutingResult(
            routes,
            dropped,
            status,
            solution.ObjectiveValue(),
            wall,
            used_hint,
            {"nodes": N, "vehicles": V, "build_s": round(build_s, 2), "recipient_pairs": n_pairs},
        )
