"""Explain *why* a visit is unplanned, from structured facts about the final plan.

For each unplanned visit we check, in order:

1. Impossible window (earliest > latest).
2. Nobody on the roster holds the required delegations / skills
   (or the recipient refuses every qualified employee).
3. Qualified employees exist but none is working / on shift in the window
   (sick, shift ended, not started yet).
4. Double staffing: fewer than two qualified employees on shift.
5. Exact gap analysis on the final plan *without moving any other visit*:
   - a feasible insertion exists  -> honest "search limit" (should be rare),
   - feasible only if travel were zero -> travel feasibility,
   - otherwise insufficient staffing, naming the binding employees, the
     shortfall in minutes and whether the blocking visits are higher priority.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import EmployeeStatus, Plan, Scenario, UnplannedReason, UnplannedVisit, Visit, fmt_time
from .travel import TravelMatrix


@dataclass(slots=True)
class Gap:
    employee_id: str
    lo: int  # earliest feasible start in this gap
    hi: int  # latest feasible start in this gap
    shortfall: int  # minutes missing (0 if feasible)
    blockers: list[str]


def _qualifies(scenario: Scenario, eid: str, visit: Visit, role: int) -> list[str]:
    e = scenario.employees[eid]
    r = scenario.recipients[visit.recipient_id]
    miss = [f"skill {s}" for s in visit.required_skills if s not in e.skills]
    if role == 0:
        miss += [f"delegation {d}" for d in visit.required_delegations if d not in e.delegations]
    if eid in r.avoid_employee_ids:
        miss.append("avoided by recipient")
    return miss


def _gaps(scenario: Scenario, travel: TravelMatrix, plan: Plan, eid: str, v: Visit, zero_travel: bool) -> list[Gap]:
    emp = scenario.employees[eid]
    route = plan.routes.get(eid)
    stops = sorted(route.stops, key=lambda s: s.start) if route else []
    start_loc = (route.start_location_id if route and route.start_location_id else emp.start_location_id)
    end_loc = emp.end_location_id
    t = (lambda a, b: 0) if zero_travel else travel.for_employee(emp).minutes
    avail = max(emp.shift_start, emp.available_from or 0)
    # Free stretches between fixed stops
    segments = []
    prev_loc, prev_end, prev_id = start_loc, avail, "shift start"
    for s in stops:
        segments.append((prev_loc, prev_end, s.location_id, s.start, prev_id, s.visit_id or s.kind))
        prev_loc, prev_end, prev_id = s.location_id, max(prev_end, s.end), s.visit_id or s.kind
    segments.append((prev_loc, prev_end, end_loc, emp.shift_end, prev_id, "shift end"))
    out = []
    for a_loc, a_end, b_loc, b_start, a_id, b_id in segments:
        a_end = max(a_end, avail)
        for iv in emp.unavailable:
            if iv.start < b_start and a_end < iv.end:
                # unavailable inside this stretch: clip
                if iv.start <= a_end:
                    a_end = iv.end
                else:
                    b_start = min(b_start, iv.start)
        lo = max(v.earliest_start, a_end + t(a_loc, v.location_id))
        hi = min(v.latest_start, b_start - v.total_duration_minutes - t(v.location_id, b_loc))
        if b_start < v.earliest_start or a_end > v.latest_start + v.total_duration_minutes:
            continue
        shortfall = max(0, lo - hi)
        out.append(Gap(eid, lo, hi, shortfall, [x for x in (a_id, b_id) if x]))
    return out


def diagnose_unplanned(scenario: Scenario, travel: TravelMatrix, plan: Plan) -> dict[str, UnplannedVisit]:
    result: dict[str, UnplannedVisit] = {}
    for vid in plan.unplanned:
        v = scenario.visits.get(vid)
        if v is None:
            continue
        result[vid] = UnplannedVisit(vid, _diagnose(scenario, travel, plan, v))
    return result


def _diagnose(scenario: Scenario, travel: TravelMatrix, plan: Plan, v: Visit) -> list[UnplannedReason]:
    window = f"{fmt_time(v.earliest_start)}-{fmt_time(v.latest_start)}"
    if v.earliest_start > v.latest_start:
        return [UnplannedReason("IMPOSSIBLE_TIME_WINDOW", f"Start window {window} is empty (conflicting interventions).")]

    roles = range(v.required_employee_count)
    qualified = {r: [e for e in scenario.employees if not _qualifies(scenario, e, v, r)] for r in roles}
    if not qualified[0]:
        missing = sorted({m for e in scenario.employees for m in _qualifies(scenario, e, v, 0)})
        needs = v.required_delegations + v.required_skills
        code = "NO_EMPLOYEE_WITH_DELEGATION" if v.required_delegations else "NO_QUALIFIED_EMPLOYEE"
        return [
            UnplannedReason(
                code,
                f"No employee on the roster holds all of {needs}.",
                {"required": needs, "missing_examples": missing[:5]},
            )
        ]

    def on_shift(eid: str) -> bool:
        e = scenario.employees[eid]
        if e.status != EmployeeStatus.WORKING:
            return False
        start = max(e.shift_start, e.available_from or 0)
        end = e.shift_end
        for iv in e.unavailable:
            if iv.end >= end and iv.start < end:
                end = min(end, iv.start)
        return start <= v.latest_start and end >= v.earliest_start + v.total_duration_minutes

    working = {r: [e for e in qualified[r] if on_shift(e)] for r in roles}
    if not working[0]:
        sick = [e for e in qualified[0] if scenario.employees[e].status != EmployeeStatus.WORKING
                or any(iv.reason.startswith("sick") for iv in scenario.employees[e].unavailable)]
        deleg = "/".join(v.required_delegations) or "the required skills"
        if sick:
            return [
                UnplannedReason(
                    "QUALIFIED_STAFF_UNAVAILABLE",
                    f"All {len(qualified[0])} employees with {deleg} are off shift or sick in {window} "
                    f"({len(sick)} reported sick).",
                    {"qualified": qualified[0], "sick": sick},
                )
            ]
        return [
            UnplannedReason(
                "EMPLOYEE_SHIFT_ENDED",
                f"No employee with {deleg} is on shift during {window}.",
                {"qualified": qualified[0]},
            )
        ]
    if v.required_employee_count == 2 and len(set(working[0]) | set(working[1])) < 2:
        return [
            UnplannedReason(
                "DOUBLE_STAFFING_UNAVAILABLE",
                f"Double staffing needs two qualified employees on shift in {window}; only "
                f"{len(set(working[0]) | set(working[1]))} available.",
                {"qualified_on_shift": sorted(set(working[0]) | set(working[1]))},
            )
        ]

    # Exact gap analysis on the final plan (other visits fixed).
    gaps = {r: [g for e in working[r] for g in _gaps(scenario, travel, plan, e, v, False)] for r in roles}
    feasible = {r: [g for g in gaps[r] if g.shortfall == 0] for r in roles}
    if v.required_employee_count == 1 and feasible[0]:
        g = min(feasible[0], key=lambda g: g.lo)
        return [
            UnplannedReason(
                "SOLVER_SEARCH_LIMIT",
                f"A feasible slot exists ({g.employee_id} at {fmt_time(g.lo)}) that the search did not use "
                f"within its time limit.",
                {"employee": g.employee_id, "start": g.lo},
            )
        ]
    if v.required_employee_count == 2:
        for g1 in feasible[0]:
            for g2 in feasible[1]:
                if g1.employee_id != g2.employee_id and max(g1.lo, g2.lo) <= min(g1.hi, g2.hi):
                    return [
                        UnplannedReason(
                            "SOLVER_SEARCH_LIMIT",
                            f"A synchronised slot exists ({g1.employee_id} + {g2.employee_id} at "
                            f"{fmt_time(max(g1.lo, g2.lo))}) that the search did not use.",
                            {"employees": [g1.employee_id, g2.employee_id]},
                        )
                    ]
        if feasible[0] or feasible[1]:
            singles = sorted({g.employee_id for r in roles for g in feasible[r]})
            return [
                UnplannedReason(
                    "DOUBLE_STAFFING_UNAVAILABLE",
                    f"Only one employee at a time is free in {window} ({', '.join(singles[:4])}); "
                    "no two qualified employees are free at the same moment.",
                    {"free_alone": singles},
                )
            ]

    zero = [g for e in working[0] for g in _gaps(scenario, travel, plan, e, v, True) if g.shortfall == 0]
    reasons = []
    if zero:
        reasons.append(
            UnplannedReason(
                "TRAVEL_FEASIBILITY",
                f"{len({g.employee_id for g in zero})} qualified employee(s) have enough free time in {window}, "
                "but not enough to also travel there and on to their next stop.",
                {"employees": sorted({g.employee_id for g in zero})[:8]},
            )
        )
    best = sorted(gaps[0], key=lambda g: g.shortfall)[:3]
    blockers = sorted({b for g in best for b in g.blockers if b in scenario.visits})
    higher = [b for b in blockers if scenario.visits[b].priority > v.priority]
    if best:
        g = best[0]
        reasons.append(
            UnplannedReason(
                "INSUFFICIENT_STAFFING",
                f"None of the {len(working[0])} qualified employees on shift has an opening in {window} "
                f"without moving other visits; the best opening ({g.employee_id}) is {g.shortfall} min too short.",
                {"qualified_on_shift": len(working[0]), "best_employee": g.employee_id, "shortfall_min": g.shortfall,
                 "blocking_visits": blockers},
            )
        )
    else:
        reasons.append(
            UnplannedReason(
                "INSUFFICIENT_STAFFING",
                f"None of the {len(working[0])} qualified employees on shift has an opening in {window} "
                "without moving other visits.",
                {"qualified_on_shift": len(working[0])},
            )
        )
    if higher:
        reasons.append(
            UnplannedReason(
                "CONFLICTING_HIGH_PRIORITY",
                f"The closest openings are occupied by higher-priority visits ({', '.join(higher[:3])}).",
                {"visits": higher},
            )
        )
    return reasons
