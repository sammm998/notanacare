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
    """All feasible insertion positions of ``stop`` into ``route``."""
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
