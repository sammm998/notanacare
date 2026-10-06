"""Exact single-route time-window feasibility for insertion heuristics.

A working route is an ordered list of :class:`Stop` (visits and breaks).
Breaks are taken at the employee's team office, so a break is an ordinary
stop with a location, a duration and a start window.

For any sequence, a forward pass gives earliest starts (ES) and a backward
pass gives latest starts (LS). With waiting allowed, inserting stop ``u`` at
position ``p`` is feasible iff ES(u) <= LS(u) on the tentative sequence, and
every start in [ES(u), LS(u)] is feasible. This is used for (pairs of)
synchronised insertions of double-staffed visits.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field

from ..travel import TravelMatrix
from .problem import VehicleSpec

NEG = -(10**9)
POS = 10**9


@dataclass(slots=True)
class Stop:
    kind: str  # "visit" | "break"
    visit_id: str | None
    role: int
    location: str
    duration: int
    earliest: int
    latest: int
    free_arrival: bool = False  # unavailability gap: stops wherever the employee is (no travel in)


@dataclass(slots=True)
class WorkRoute:
    vehicle: VehicleSpec
    stops: list[Stop] = field(default_factory=list)
    # Forward/backward passes of the current stops (see _passes). Stops are never
    # edited in place, so the identity of the stop objects is a safe cache key.
    _pass: tuple | None = field(default=None, repr=False, compare=False)

    def visit_stops(self) -> list[Stop]:
        return [s for s in self.stops if s.kind == "visit"]


def _travel_chain(stops: list[Stop], v: VehicleSpec, tm: TravelMatrix) -> tuple[list[int], int]:
    """Travel time *into* each stop and from the last stop to the end location."""
    tm = v.travel or tm
    into: list[int] = []
    cur = v.start_location
    for s in stops:
        into.append(0 if s.free_arrival else tm.minutes(cur, s.location))
        cur = s.location
    return into, tm.minutes(cur, v.end_location)


def forward(stops: list[Stop], v: VehicleSpec, tm: TravelMatrix) -> list[int] | None:
    """Earliest feasible starts, or None if the sequence is infeasible."""
    into, to_end = _travel_chain(stops, v, tm)
    t = v.start_time
    out = []
    for s, tr in zip(stops, into):
        start = max(t + tr, s.earliest)
        if start > s.latest:
            return None
        out.append(start)
        t = start + s.duration
    if t + to_end > v.end_time:
        return None
    return out


def backward(stops: list[Stop], v: VehicleSpec, tm: TravelMatrix) -> list[int]:
    into, to_end = _travel_chain(stops, v, tm)
    out = [0] * len(stops)
    nxt = v.end_time - to_end  # latest time we may finish the last stop
    for k in range(len(stops) - 1, -1, -1):
        s = stops[k]
        ls = min(s.latest, nxt - s.duration)
        out[k] = ls
        nxt = ls - into[k]
    return out


def _passes(route: WorkRoute, tm: TravelMatrix) -> tuple:
    """(earliest starts or None, latest starts, travel into each stop, travel to end, visit ids), cached."""
    v = route.vehicle
    stops = route.stops
    # The cache keeps the stop objects alive, so identity cannot be reused by new objects.
    key = (v.travel or tm, v.start_time, v.end_time, v.start_location, v.end_location)
    c = route._pass
    if c is not None and len(c[0]) == len(stops) and c[1] == key and all(map(operator.is_, c[0], stops)):
        return c[2]
    into, to_end = _travel_chain(stops, v, tm)
    t = v.start_time
    es: list[int] | None = []
    for st, tr in zip(stops, into):
        start = max(t + tr, st.earliest)
        if start > st.latest:
            es = None
            break
        es.append(start)
        t = start + st.duration
    if es is not None and t + to_end > v.end_time:
        es = None
    ls = [0] * len(stops)
    nxt = v.end_time - to_end
    for k in range(len(stops) - 1, -1, -1):
        st = stops[k]
        ls[k] = min(st.latest, nxt - st.duration)
        nxt = ls[k] - into[k]
    val = (es, ls, into, to_end, frozenset(st.visit_id for st in stops if st.visit_id))
    route._pass = (tuple(stops), key, val)
    return val


def route_visit_ids(route: WorkRoute, tm: TravelMatrix) -> frozenset:
    return _passes(route, tm)[4]


def route_travel(stops: list[Stop], v: VehicleSpec, tm: TravelMatrix) -> int:
    into, to_end = _travel_chain(stops, v, tm)
    return sum(into) + to_end


@dataclass(slots=True)
class InsertionOption:
    employee_id: str
    position: int
    lo: int  # feasible start interval of the inserted stop
    hi: int
    delta_travel: int


def insertion_options(route: WorkRoute, stop: Stop, tm: TravelMatrix) -> list[InsertionOption]:
    """All feasible insertion positions of ``stop`` into ``route``.

    Linear time: the earliest starts (forward pass) and latest starts (backward
    pass) of the current route are computed once. Inserting at ``p`` leaves
    the prefix unchanged, and the suffix is feasible iff the next stop can
    still start by its latest start, so every position is checked in O(1).
    Gives exactly the same result as re-running both passes per position
    (:func:`insertion_options_reference`).
    """
    v = route.vehicle
    opts: list[InsertionOption] = []
    if stop.earliest > stop.latest:
        return opts
    stops = route.stops
    es, ls, into, to_end, _ = _passes(route, tm)
    if es is None:  # infeasible already (e.g. after traffic): keep the exact slow path
        return insertion_options_reference(route, stop, tm)
    vtm = v.travel or tm
    n = len(stops)
    loc, dur = stop.location, stop.duration
    M, idx = getattr(vtm, "_minutes", None), getattr(vtm, "index", None)
    if M is not None and idx is not None:  # direct matrix access (hot loop)
        iu = idx[loc]
        row_u = M[iu]
        t_from = lambda a: M[idx[a]][iu]  # noqa: E731
        t_to = lambda b: row_u[idx[b]]  # noqa: E731
    else:
        t_from = lambda a: vtm.minutes(a, loc)  # noqa: E731
        t_to = lambda b: vtm.minutes(loc, b)  # noqa: E731
    for p in range(n + 1):
        if p > 0 and stops[p - 1].earliest > stop.latest:
            break
        if p < n and stops[p].latest + stops[p].duration < stop.earliest:
            continue
        if p > 0:
            prev = stops[p - 1]
            depart, prev_loc = es[p - 1] + prev.duration, prev.location
        else:
            depart, prev_loc = v.start_time, v.start_location
        t_in = t_from(prev_loc)
        start = max(depart + t_in, stop.earliest)
        if start > stop.latest:
            continue
        if p < n:
            nxt = stops[p]
            t_out = 0 if nxt.free_arrival else t_to(nxt.location)
            if max(start + dur + t_out, nxt.earliest) > ls[p]:
                continue
            latest = min(stop.latest, ls[p] - t_out - dur)
            delta = t_in + t_out - into[p]
        else:
            t_out = t_to(v.end_location)
            if start + dur + t_out > v.end_time:
                continue
            latest = min(stop.latest, v.end_time - t_out - dur)
            delta = t_in + t_out - to_end
        if start > latest:
            continue
        opts.append(InsertionOption(employee_id=v.employee_id, position=p, lo=start, hi=latest, delta_travel=delta))
    return opts


def insertion_options_reference(route: WorkRoute, stop: Stop, tm: TravelMatrix) -> list[InsertionOption]:
    """Reference implementation: re-runs the forward and backward passes per position (O(n^2))."""
    v = route.vehicle
    opts: list[InsertionOption] = []
    if stop.earliest > stop.latest:
        return opts
    base_travel = route_travel(route.stops, v, tm)
    n = len(route.stops)
    for p in range(n + 1):
        # Quick reject on windows of neighbours.
        if p > 0 and route.stops[p - 1].earliest > stop.latest:
            break
        if p < n and route.stops[p].latest + route.stops[p].duration < stop.earliest:
            continue
        cand = route.stops[:p] + [stop] + route.stops[p:]
        es = forward(cand, v, tm)
        if es is None:
            continue
        ls = backward(cand, v, tm)
        if es[p] > ls[p]:
            continue
        opts.append(
            InsertionOption(
                employee_id=v.employee_id,
                position=p,
                lo=es[p],
                hi=ls[p],
                delta_travel=route_travel(cand, v, tm) - base_travel,
            )
        )
    return opts


def feasible(route: WorkRoute, tm: TravelMatrix) -> bool:
    return forward(route.stops, route.vehicle, tm) is not None
