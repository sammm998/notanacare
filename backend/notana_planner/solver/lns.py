"""Enhanced repair: ejection-chain insertion for leftover visits.

For every unplanned visit ``u`` (highest priority first) we look at placed
single-staffed visits ``x`` that occupy a qualified employee during ``u``'s
window. We tentatively remove ``x``, insert ``u`` and re-insert ``x``
anywhere else. The move is kept if both succeed (one more visit planned), or
-- only when ``u`` has strictly higher priority -- if ``x`` cannot be
re-inserted (a deliberate, recorded trade-off). Everything else is reverted.
All feasibility checks are the exact single-route checks of :mod:`routes`.
"""

from __future__ import annotations

import time

from .construction import Constructor
from .problem import PlanningProblem, Task


def _snapshot(ctor: Constructor, eids: set[str]) -> dict:
    return {
        "routes": {e: list(ctor.routes[e].stops) for e in eids},
        "placements": dict(ctor.placements),
        "busy": {k: list(v) for k, v in ctor.recipient_busy.items()},
    }


def _restore(ctor: Constructor, snap: dict) -> None:
    for e, stops in snap["routes"].items():
        ctor.routes[e].stops = stops
    ctor.placements = snap["placements"]
    ctor.recipient_busy = snap["busy"]


def _remove(ctor: Constructor, vid: str) -> set[str]:
    pl = ctor.placements.pop(vid, None)
    touched: set[str] = set()
    if pl is None:
        return touched
    for eid, start in pl.roles.values():
        r = ctor.routes[eid]
        r.stops = [s for s in r.stops if s.visit_id != vid]
        touched.add(eid)
    busy = ctor.recipient_busy.get(pl.task.recipient_id, [])
    starts = [s for _, s in pl.roles.values()]
    if starts:
        iv = (min(starts), max(starts) + pl.task.duration)
        if iv in busy:
            busy.remove(iv)
    return touched


def enhanced_repair(
    problem: PlanningProblem,
    ctor: Constructor,
    unplanned: set[str],
    rounds: int = 2,
    time_budget_s: float = 8.0,
) -> tuple[set[str], dict]:
    t0 = time.perf_counter()
    tasks = {t.visit_id: t for t in problem.tasks}
    stats = {"rounds": 0, "planned_by_ejection": 0, "displaced_lower_priority": [], "tried": len(unplanned)}
    left = set(unplanned)
    for _ in range(rounds):
        stats["rounds"] += 1
        progress = False
        for vid in sorted(left, key=lambda v: (-tasks[v].priority, tasks[v].latest - tasks[v].earliest)):
            if time.perf_counter() - t0 > time_budget_s:
                break
            u = tasks[vid]
            if ctor.try_insert(u):
                left.discard(vid)
                progress = True
                continue
            if _eject_and_insert(ctor, u, tasks, left, stats):
                progress = True
        if not progress:
            break
    stats["seconds"] = round(time.perf_counter() - t0, 2)
    return left, stats


def _candidates(ctor: Constructor, u: Task, tasks: dict[str, Task]) -> list[str]:
    allowed = set(u.allowed[0]) | (set(u.allowed[1]) if u.roles == 2 else set())
    tm = ctor.tm
    scored = []
    for vid, pl in ctor.placements.items():
        t = tasks.get(vid)
        if t is None or t.pinned or t.roles != 1:
            continue
        eid, start = pl.roles[0]
        if eid not in allowed:
            continue
        if start + t.duration < u.earliest or start > u.latest + u.duration:
            continue
        scored.append((tm.minutes(t.location_id, u.location_id), vid))
    scored.sort()
    return [v for _, v in scored[:40]]


def _eject_and_insert(ctor: Constructor, u: Task, tasks: dict[str, Task], left: set[str], stats: dict) -> bool:
    for xid in _candidates(ctor, u, tasks):
        x = tasks[xid]
        pl = ctor.placements[xid]
        eids = {e for e, _ in pl.roles.values()} | set(u.allowed[0]) | (set(u.allowed[1]) if u.roles == 2 else set())
        snap = _snapshot(ctor, eids | set(x.allowed[0]))
        _remove(ctor, xid)
        if not ctor.try_insert(u):
            _restore(ctor, snap)
            continue
        if ctor.try_insert(x):
            left.discard(u.visit_id)
            stats["planned_by_ejection"] += 1
            return True
        if u.priority > x.priority:
            left.discard(u.visit_id)
            left.add(xid)
            stats["displaced_lower_priority"].append({"inserted": u.visit_id, "displaced": xid})
            return True
        _restore(ctor, snap)
    return False
