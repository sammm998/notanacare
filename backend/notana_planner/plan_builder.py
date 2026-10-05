"""Assemble a :class:`Plan` from solver output, frozen prefixes and kept routes."""

from __future__ import annotations

import datetime as dt
import uuid

from .domain import Assignment, Plan, Route, RouteStop, Scenario, UnplannedVisit, VisitStatus
from .solver.engine import SolveOutput
from .solver.problem import PlanningProblem
from .travel import TravelMatrix


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_plan_id() -> str:
    return "P-" + uuid.uuid4().hex[:10]


def assemble_plan(
    scenario: Scenario,
    travel: TravelMatrix,
    problem: PlanningProblem,
    output: SolveOutput,
    *,
    strategy: str,
    previous: Plan | None = None,
    frozen: dict[str, list[RouteStop]] | None = None,
    keep_routes: set[str] | None = None,
    clock: int | None = None,
) -> Plan:
    frozen = frozen or {}
    keep_routes = keep_routes or set()
    vehicles = {v.employee_id: v for v in problem.vehicles}
    routes: dict[str, Route] = {}

    for eid, emp in scenario.employees.items():
        if eid in keep_routes and previous is not None and eid in previous.routes:
            # Untouched route: same visits, same times. Started stops are locked;
            # travel data of future legs is refreshed to the current conditions
            # (feasibility is then checked by the validator, never assumed).
            old = previous.routes[eid]
            stops = []
            loc = old.start_location_id or emp.start_location_id
            for s0 in old.stops:
                started = clock is not None and s0.start <= clock
                st = _copy_stop(s0, locked=True if started else None)
                if not st.locked:
                    st.travel_from_prev = 0 if st.kind == "unavailable" else travel.minutes(loc, st.location_id)
                    st.prev_location_id = loc
                stops.append(st)
                loc = st.location_id
            route_end = old.route_end
            to_end = travel.minutes(loc, old.end_location_id or emp.end_location_id)
            if stops and route_end is not None:
                route_end = max(route_end, stops[-1].end + to_end)
            routes[eid] = Route(
                employee_id=eid,
                stops=stops,
                route_start=old.route_start,
                route_end=route_end,
                start_location_id=old.start_location_id,
                end_location_id=old.end_location_id,
                travel_to_end=to_end,
            )
            continue
        prefix = [_copy_stop(s, locked=True) for s in frozen.get(eid, [])]
        prev_route = previous.routes.get(eid) if previous else None
        route = Route(
            employee_id=eid,
            stops=list(prefix),
            route_start=prev_route.route_start if (prefix and prev_route) else None,
            start_location_id=(prev_route.start_location_id if (prefix and prev_route) else emp.start_location_id),
            end_location_id=emp.end_location_id,
        )
        v = vehicles.get(eid)
        solved = output.routes.get(eid, []) if v is not None else []
        if v is not None:
            loc = v.start_location
            for s in solved:
                kind = "visit" if s.kind == "visit" else v.breaks[s.role].kind
                route.stops.append(
                    RouteStop(
                        kind=kind,
                        visit_id=s.visit_id,
                        role=s.role,
                        location_id=s.location,
                        start=s.start,
                        end=s.start + s.duration,
                        travel_from_prev=0 if kind == "unavailable" else travel.minutes(loc, s.location),
                        prev_location_id=loc,
                    )
                )
                loc = s.location
            if route.route_start is None:
                route.route_start = output.route_start.get(eid, v.start_time)
            if solved or prefix:
                route.route_end = output.route_end.get(eid)
                route.travel_to_end = travel.minutes(loc, v.end_location)
            if not solved and not prefix:
                route.route_start = None
                route.route_end = None
        elif prefix:
            # Not part of the problem (e.g. sick): keep locked work, then go home.
            last = prefix[-1]
            route.travel_to_end = travel.minutes(last.location_id, emp.end_location_id)
            route.route_end = None
        routes[eid] = route

    assignments: dict[str, Assignment] = {}
    by_visit: dict[str, list[tuple[int, str, RouteStop]]] = {}
    for eid, r in routes.items():
        for s in r.stops:
            if s.kind == "visit" and s.visit_id:
                by_visit.setdefault(s.visit_id, []).append((s.role, eid, s))
    for vid, occ in by_visit.items():
        occ.sort(key=lambda x: (x[0], x[1]))
        assignments[vid] = Assignment(
            visit_id=vid,
            employee_ids=[e for _, e, _ in occ],
            start=min(s.start for _, _, s in occ),
            end=max(s.end for _, _, s in occ),
            starts={e: s.start for _, e, s in occ},
        )

    # A double-staffed visit that lost one of its employees (e.g. a pinned role
    # the solver had to drop) is released as a whole - never left half-staffed.
    for vid, a in list(assignments.items()):
        v = scenario.visits.get(vid)
        started = clock is not None and a.start <= clock
        if v is not None and len(a.employee_ids) < v.required_employee_count and not started:
            for eid in a.employee_ids:
                routes[eid].stops = [s for s in routes[eid].stops if s.visit_id != vid]
                _refresh_travel(routes[eid], travel, scenario.employees[eid].start_location_id, clock)
            del assignments[vid]
            output.stats.setdefault("released_incomplete_doubles", []).append(vid)

    unplanned = {
        vid: UnplannedVisit(vid, [])
        for vid, v in scenario.visits.items()
        if vid not in assignments and v.status != VisitStatus.CANCELLED
    }
    locked = sorted(
        {s.visit_id for r in routes.values() for s in r.stops if s.locked and s.visit_id}
        | {vid for vid, v in scenario.visits.items() if v.locked and vid in assignments}
    )
    return Plan(
        id=new_plan_id(),
        scenario_id=scenario.id,
        created_at=_now(),
        strategy=strategy,
        assignments=assignments,
        routes=routes,
        unplanned=unplanned,
        locked_visit_ids=locked,
        clock=clock,
        solver_stats=output.stats,
        travel_source=travel.source,
        parent_plan_id=previous.id if previous else None,
    )


def _refresh_travel(route: Route, travel: TravelMatrix, default_start: str, clock: int | None) -> None:
    """Recompute recorded travel of future legs after a stop was removed."""
    loc = route.start_location_id or default_start
    for st in sorted(route.stops, key=lambda s: s.start):
        if not st.locked and not (clock is not None and st.start <= clock):
            st.travel_from_prev = 0 if st.kind == "unavailable" else travel.minutes(loc, st.location_id)
            st.prev_location_id = loc
        loc = st.location_id
    if route.stops and route.route_end is not None:
        route.travel_to_end = travel.minutes(loc, route.end_location_id or default_start)


def _copy_stop(s: RouteStop, locked: bool | None = None) -> RouteStop:
    return RouteStop(
        kind=s.kind,
        visit_id=s.visit_id,
        role=s.role,
        location_id=s.location_id,
        start=s.start,
        end=s.end,
        travel_from_prev=s.travel_from_prev,
        prev_location_id=s.prev_location_id,
        locked=s.locked if locked is None else locked,
    )
