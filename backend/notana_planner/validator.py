"""Independent plan validator.

This module deliberately imports **nothing** from :mod:`notana_planner.solver`.
It re-derives every rule from the scenario (visits, employees, recipients),
the active travel matrix and the plan itself, so an optimizer bug cannot
"approve" its own output. A plan is VALID only if this returns no hard errors.

Checked (hard):
  references, completeness, double booking of a visit, route time
  consistency (end - start == full visit duration, travel between
  consecutive stops, departure / return travel), no overlap per employee,
  visit start windows, shift bounds (+ approved overtime), availability
  (sickness, delays, unavailable intervals), skills (all staff), delegations
  (lead holds all), recipient avoid-list, strict gender requirement,
  pet allergy, smoke-free workplace, required language (lead),
  Arbetstidslagen (max 5 h continuous work, max daily work, 11 h dygnsvila), double-staffing count, distinct
  employees and start synchronisation, mandatory breaks, recipient
  double-booking, locked / started / completed visits unchanged vs the
  reference plan, max workload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .domain import EmployeeStatus, Plan, RouteStop, Scenario
from .travel import TravelMatrix


@dataclass(slots=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "hard"  # "hard" | "warning"
    visit_id: str | None = None
    employee_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "visit_id": self.visit_id,
            "employee_id": self.employee_id,
            "details": self.details,
        }


@dataclass(slots=True)
class ValidationReport:
    valid: bool
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
    checks: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "status": "VALID" if self.valid else "INVALID",
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [e.to_dict() for e in self.errors[:500]],
            "warnings": [w.to_dict() for w in self.warnings[:500]],
            "errors_by_code": _count_codes(self.errors),
            "checks": self.checks,
        }


@dataclass(slots=True)
class _Rest:
    start: int
    end: int


def _count_codes(items: list[ValidationIssue]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i.code] = out.get(i.code, 0) + 1
    return out


def _hhmm(m: int | None) -> str:
    if m is None:
        return "--:--"
    return f"{m // 60:02d}:{m % 60:02d}"


def validate_plan(
    scenario: Scenario,
    travel: TravelMatrix,
    plan: Plan,
    *,
    sync_tolerance: int = 0,
    max_overtime: int = 0,
    reference: Plan | None = None,
    clock: int | None = None,
) -> ValidationReport:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    checks: dict[str, int] = {}

    def err(code: str, msg: str, **kw: Any) -> None:
        errors.append(ValidationIssue(code, msg, "hard", **kw))

    def warn(code: str, msg: str, **kw: Any) -> None:
        warnings.append(ValidationIssue(code, msg, "warning", **kw))

    def tick(name: str, n: int = 1) -> None:
        checks[name] = checks.get(name, 0) + n

    visits = scenario.visits
    emps = scenario.employees
    locked_ids = set(plan.locked_visit_ids)

    # ------------------------------------------------------------------ refs
    seen_in_routes: dict[str, list[tuple[str, RouteStop]]] = {}
    for eid, route in plan.routes.items():
        tick("references")
        if eid not in emps:
            err("UNKNOWN_EMPLOYEE", f"Route for unknown employee {eid}", employee_id=eid)
            continue
        for st in route.stops:
            if st.kind != "visit":
                continue
            if st.visit_id not in visits:
                err("UNKNOWN_VISIT", f"{eid} has unknown visit {st.visit_id}", employee_id=eid)
                continue
            seen_in_routes.setdefault(st.visit_id, []).append((eid, st))

    for vid, a in plan.assignments.items():
        tick("references")
        if vid not in visits:
            err("UNKNOWN_VISIT", f"Assignment for unknown visit {vid}", visit_id=vid)
            continue
        for eid in a.employee_ids:
            if eid not in emps:
                err("UNKNOWN_EMPLOYEE", f"{vid} assigned to unknown {eid}", visit_id=vid, employee_id=eid)
        in_routes = sorted(e for e, _ in seen_in_routes.get(vid, []))
        if in_routes != sorted(a.employee_ids):
            err(
                "ASSIGNMENT_ROUTE_MISMATCH",
                f"{vid}: assignment says {sorted(a.employee_ids)}, routes contain {in_routes}",
                visit_id=vid,
            )
        if vid in plan.unplanned:
            err("PLANNED_AND_UNPLANNED", f"{vid} is both planned and unplanned", visit_id=vid)
    for vid in seen_in_routes:
        if vid not in plan.assignments:
            err("ROUTE_WITHOUT_ASSIGNMENT", f"{vid} appears in a route but has no assignment", visit_id=vid)
    for vid, v in visits.items():
        tick("completeness")
        if v.status.value == "cancelled":
            if vid in plan.assignments and vid not in locked_ids:
                err("CANCELLED_VISIT_PLANNED", f"{vid} is cancelled but still planned", visit_id=vid)
            continue
        if vid not in plan.assignments and vid not in plan.unplanned:
            err("VISIT_MISSING", f"{vid} is neither planned nor reported unplanned", visit_id=vid)

    # --------------------------------------------------------- per employee
    for eid, route in plan.routes.items():
        emp = emps.get(eid)
        if emp is None:
            continue
        stops = sorted(route.stops, key=lambda s: (s.start, s.end))
        if [id(s) for s in stops] != [id(s) for s in route.stops]:
            err("ROUTE_NOT_CHRONOLOGICAL", f"{eid}: stops are not in chronological order", employee_id=eid)
        unlocked_visits = [s for s in stops if s.kind == "visit" and not s.locked]
        if emp.status != EmployeeStatus.WORKING and unlocked_visits:
            err(
                "EMPLOYEE_NOT_WORKING",
                f"{eid} is {emp.status.value} but has {len(unlocked_visits)} non-locked visits",
                employee_id=eid,
            )
        etm = travel.for_employee(emp)  # car / bike
        start_loc = route.start_location_id or emp.start_location_id
        end_loc = route.end_location_id or emp.end_location_id
        hard_end = emp.shift_end + max(0, max_overtime)
        if route.route_start is not None and route.route_start < emp.shift_start:
            err("BEFORE_SHIFT", f"{eid} leaves at {_hhmm(route.route_start)} before shift start {_hhmm(emp.shift_start)}", employee_id=eid)
        tick("shift")

        prev_loc = start_loc
        prev_end = route.route_start if route.route_start is not None else emp.shift_start
        prev_kind = "start"
        for st in stops:
            tick("route_consistency")
            if st.end < st.start:
                err("NEGATIVE_DURATION", f"{eid}: stop {st.visit_id or st.kind} ends before it starts", employee_id=eid)
            # Shift bounds
            if st.start < emp.shift_start or st.end > hard_end:
                if not st.locked:
                    err(
                        "OUTSIDE_SHIFT",
                        f"{eid}: {st.visit_id or st.kind} {_hhmm(st.start)}-{_hhmm(st.end)} outside shift "
                        f"{_hhmm(emp.shift_start)}-{_hhmm(hard_end)}",
                        employee_id=eid,
                        visit_id=st.visit_id,
                    )
            # Travel feasibility (historic legs into locked stops are not re-checked:
            # they were driven under the conditions of that time).
            # An unavailability gap (split shift, appointment) starts wherever the
            # employee is; work resumes from the gap's location (the team office).
            t = 0 if st.kind == "unavailable" else etm.minutes(prev_loc, st.location_id)
            historic = st.locked or (clock is not None and st.start <= clock)
            if not historic:
                ready = prev_end
                if emp.available_from is not None:
                    ready = max(ready, emp.available_from)
                    if st.start < emp.available_from:
                        err(
                            "BEFORE_AVAILABLE",
                            f"{eid}: {st.visit_id or st.kind} at {_hhmm(st.start)} but employee only available from {_hhmm(emp.available_from)}",
                            employee_id=eid,
                            visit_id=st.visit_id,
                        )
                if ready + t > st.start:
                    code = "OVERLAP" if prev_end > st.start else "TRAVEL_INFEASIBLE"
                    err(
                        code,
                        f"{eid}: cannot reach {st.visit_id or st.kind} at {_hhmm(st.start)} "
                        f"(free {_hhmm(ready)} + {t} min travel from {prev_loc})",
                        employee_id=eid,
                        visit_id=st.visit_id,
                        details={"prev": prev_kind, "travel": t, "ready": ready, "start": st.start},
                    )
                tick("travel")
                if st.travel_from_prev != t or st.prev_location_id != prev_loc:
                    err(
                        "ROUTE_DATA_INCONSISTENT",
                        f"{eid}: recorded travel into {st.visit_id or st.kind} is {st.travel_from_prev} min from "
                        f"{st.prev_location_id}, recomputed {t} min from {prev_loc}",
                        employee_id=eid,
                        visit_id=st.visit_id,
                    )
            elif prev_end > st.start:
                err("OVERLAP", f"{eid}: started stops overlap at {_hhmm(st.start)}", employee_id=eid)
            prev_loc, prev_end, prev_kind = st.location_id, st.end, st.visit_id or st.kind

            if st.kind == "visit" and st.visit_id in visits:
                v = visits[st.visit_id]
                tick("duration")
                if st.end - st.start != v.total_duration_minutes:
                    err(
                        "DURATION_NOT_RESERVED",
                        f"{st.visit_id} on {eid} reserves {st.end - st.start} min, requires {v.total_duration_minutes}",
                        visit_id=v.id,
                        employee_id=eid,
                    )
                if st.location_id != v.location_id:
                    err("WRONG_LOCATION", f"{v.id} on {eid} at wrong location", visit_id=v.id, employee_id=eid)
                # Availability windows (sick from / unavailable / split gaps)
                for iv in emp.unavailable:
                    if st.start < iv.end and iv.start < st.end and not st.locked:
                        err(
                            "DURING_UNAVAILABILITY",
                            f"{v.id} on {eid} {_hhmm(st.start)}-{_hhmm(st.end)} overlaps unavailable "
                            f"{_hhmm(iv.start)}-{_hhmm(iv.end)} ({iv.reason})",
                            visit_id=v.id,
                            employee_id=eid,
                        )
                tick("availability")

        # Return to end location
        if stops or route.route_end is not None:
            t_end = etm.minutes(prev_loc, end_loc)
            last_locked = bool(stops) and stops[-1].locked and route.route_end is None
            if route.route_end is None:
                if not last_locked:
                    err("ROUTE_END_MISSING", f"{eid}: route has no return time", employee_id=eid)
            else:
                if prev_end + t_end > route.route_end:
                    err(
                        "RETURN_INFEASIBLE",
                        f"{eid}: cannot return to {end_loc} by {_hhmm(route.route_end)}",
                        employee_id=eid,
                    )
                if route.route_end > hard_end:
                    err(
                        "OUTSIDE_SHIFT",
                        f"{eid}: returns {_hhmm(route.route_end)} after shift end {_hhmm(hard_end)}",
                        employee_id=eid,
                    )
                elif route.route_end > emp.shift_end:
                    warn("OVERTIME", f"{eid}: {route.route_end - emp.shift_end} min approved overtime", employee_id=eid)

        # Arbetstidslagen: continuous work, daily work, dygnsvila (re-derived from the route)
        rules = scenario.rules
        if rules.enabled and stops and route.route_start is not None:
            day_end = route.route_end if route.route_end is not None else max(s.end for s in stops)
            # Rests: breaks in the route plus the employee's own unpaid gaps (a split-shift
            # gap is a rest even when a re-plan started after it began and no stop shows it).
            spans = [(s.start, s.end) for s in stops if s.kind in ("break", "unavailable") and s.end - s.start >= 30]
            spans += [(max(iv.start, route.route_start), min(iv.end, day_end)) for iv in emp.unavailable
                      if iv.end - iv.start >= 30 and iv.start < day_end and iv.end > route.route_start]
            merged: list[list[int]] = []
            for a, b in sorted(x for x in spans if x[1] > x[0]):
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            rests = [_Rest(a, b) for a, b in merged]
            seg_start, worked = route.route_start, 0
            for s in rests + [None]:
                seg_end = s.start if s is not None else day_end
                tick("working_time")
                if seg_end - seg_start > rules.max_continuous_work_min and not (clock is not None and seg_end <= clock):
                    err(
                        "ATL_CONTINUOUS_WORK",
                        f"{eid}: works {seg_end - seg_start} min in a row {_hhmm(seg_start)}-{_hhmm(seg_end)} "
                        f"without a rest (max {rules.max_continuous_work_min}, ATL 15 §)",
                        employee_id=eid,
                    )
                worked += max(0, seg_end - seg_start)
                if s is not None:
                    seg_start = max(seg_start, s.end)
            if worked > rules.max_daily_work_min:
                err(
                    "MAX_DAILY_WORK",
                    f"{eid}: {worked} min of work, max {rules.max_daily_work_min}",
                    employee_id=eid,
                )
            if day_end - route.route_start > 24 * 60 - rules.min_daily_rest_min:
                err(
                    "DAILY_REST_VIOLATED",
                    f"{eid}: day spans {_hhmm(route.route_start)}-{_hhmm(day_end)}, leaving less than "
                    f"{rules.min_daily_rest_min // 60} h dygnsvila (ATL 13 §)",
                    employee_id=eid,
                )
            if emp.latest_end_by_rest is not None and day_end > emp.latest_end_by_rest:
                err(
                    "DAILY_REST_VIOLATED",
                    f"{eid}: ends {_hhmm(day_end)}, but tomorrow's shift needs the day to end by "
                    f"{_hhmm(emp.latest_end_by_rest)} (11 h dygnsvila, ATL 13 §)",
                    employee_id=eid,
                )

        # Mandatory meal breaks
        if emp.status == EmployeeStatus.WORKING and stops:
            break_stops = [s for s in stops if s.kind == "break"]
            for rule in emp.breaks:
                tick("breaks")
                ok = [b for b in break_stops if b.end - b.start >= rule.duration_minutes]
                if not ok:
                    # Employee stops working (sick / sent home) before the break could
                    # have ended: the break requirement lapses with the rest of the shift.
                    stopped = any(
                        iv.end >= emp.shift_end and iv.start <= rule.latest_start + rule.duration_minutes
                        for iv in emp.unavailable
                    )
                    if stopped:
                        continue
                    err(
                        "BREAK_MISSING",
                        f"{eid}: mandatory {rule.duration_minutes} min break missing",
                        employee_id=eid,
                    )
                    continue
                inside = [b for b in ok if rule.earliest_start <= b.start <= rule.latest_start]
                if not inside:
                    warn(
                        "BREAK_WINDOW_SHIFTED",
                        f"{eid}: break at {_hhmm(ok[0].start)} outside window "
                        f"{_hhmm(rule.earliest_start)}-{_hhmm(rule.latest_start)} (availability changed)",
                        employee_id=eid,
                    )
        # Max workload
        if emp.max_workload_minutes is not None:
            care = sum(s.end - s.start for s in stops if s.kind == "visit")
            if care > emp.max_workload_minutes:
                err("MAX_WORKLOAD", f"{eid}: {care} care minutes > max {emp.max_workload_minutes}", employee_id=eid)

    # ------------------------------------------------------------ per visit
    recipient_intervals: dict[str, list[tuple[int, int, str]]] = {}
    for vid, a in plan.assignments.items():
        v = visits.get(vid)
        if v is None:
            continue
        r = scenario.recipients[v.recipient_id]
        occ = seen_in_routes.get(vid, [])
        tick("staffing")
        if len(set(a.employee_ids)) != len(a.employee_ids):
            err("SAME_EMPLOYEE_TWICE", f"{vid}: the same employee is assigned twice", visit_id=vid)
        if len(a.employee_ids) != v.required_employee_count:
            err(
                "WRONG_STAFF_COUNT",
                f"{vid} needs {v.required_employee_count} employee(s), has {len(a.employee_ids)}",
                visit_id=vid,
            )
        starts = [st.start for _, st in occ]
        if starts:
            if max(starts) - min(starts) > sync_tolerance:
                err(
                    "DOUBLE_STAFFING_NOT_SYNCHRONISED",
                    f"{vid}: staff start at {', '.join(_hhmm(s) for s in sorted(starts))} "
                    f"(tolerance {sync_tolerance} min)",
                    visit_id=vid,
                )
            tick("time_window")
            locked = vid in locked_ids
            for s0 in starts:
                if not locked and (s0 < v.earliest_start or s0 > v.latest_start):
                    err(
                        "TIME_WINDOW_VIOLATED",
                        f"{vid} starts {_hhmm(s0)}, allowed {_hhmm(v.earliest_start)}-{_hhmm(v.latest_start)} "
                        f"({v.timing.value} window)",
                        visit_id=vid,
                    )
            recipient_intervals.setdefault(v.recipient_id, []).append(
                (min(starts), max(starts) + v.total_duration_minutes, vid)
            )
        tick("qualifications")
        for eid in a.employee_ids:
            emp = emps.get(eid)
            if emp is None:
                continue
            missing = [s for s in v.required_skills if s not in emp.skills]
            if missing:
                err("MISSING_SKILL", f"{eid} lacks skill(s) {missing} for {vid}", visit_id=vid, employee_id=eid)
            if eid in r.avoid_employee_ids:
                err("AVOIDED_EMPLOYEE", f"{r.id} must not be visited by {eid} ({vid})", visit_id=vid, employee_id=eid)
            in_scope = r.gender_scope == "all" or v.intimate_care
            if r.gender_strict and r.gender_preference and in_scope and emp.gender != r.gender_preference:
                err(
                    "GENDER_REQUIREMENT_VIOLATED",
                    f"{r.id} requires {'female' if r.gender_preference == 'F' else 'male'} staff for "
                    f"{'all visits' if r.gender_scope == 'all' else 'intimate care'}; {eid} on {vid} is not",
                    visit_id=vid,
                    employee_id=eid,
                )
            pets = sorted(set(r.pets) & set(emp.pet_allergies))
            if pets:
                err("PET_ALLERGY", f"{eid} is allergic to {', '.join(pets)} living at {r.id} ({vid})", visit_id=vid, employee_id=eid)
            if r.smokes and emp.avoid_smoking:
                err("SMOKING_EXPOSURE", f"{eid} must not work in a smoking home ({r.id}, {vid})", visit_id=vid, employee_id=eid)
        tick("wishes")
        if r.language_required and a.employee_ids:
            lead = emps.get(a.employee_ids[0])
            if lead is not None and not set(r.languages) & set(lead.languages):
                err(
                    "LANGUAGE_REQUIRED",
                    f"{r.id} needs staff speaking {'/'.join(r.languages)}; lead {lead.id} on {vid} does not",
                    visit_id=vid,
                    employee_id=lead.id,
                )
        if v.required_delegations and a.employee_ids:
            lead = emps.get(a.employee_ids[0])
            holders = [
                e for e in a.employee_ids if e in emps and all(d in emps[e].delegations for d in v.required_delegations)
            ]
            if lead is None or not all(d in lead.delegations for d in v.required_delegations):
                if holders:
                    warn(
                        "DELEGATION_NOT_ON_LEAD",
                        f"{vid}: delegation holder {holders[0]} is not listed as lead",
                        visit_id=vid,
                    )
                else:
                    err(
                        "MISSING_DELEGATION",
                        f"No employee on {vid} holds delegation(s) {v.required_delegations}",
                        visit_id=vid,
                    )

    for rid, ivs in recipient_intervals.items():
        ivs.sort()
        for (s1, e1, v1), (s2, e2, v2) in zip(ivs, ivs[1:]):
            tick("recipient_overlap")
            if s2 < e1:
                err("RECIPIENT_DOUBLE_BOOKED", f"{rid}: {v1} and {v2} overlap", visit_id=v2)

    # ---------------------------------------------------- locked vs reference
    if reference is not None:
        for vid, ra in reference.assignments.items():
            started = clock is not None and ra.start <= clock
            user_locked = vid in visits and visits[vid].locked
            if not (started or user_locked):
                continue
            tick("locked")
            na = plan.assignments.get(vid)
            label = "completed" if clock is not None and ra.end <= clock else "started" if started else "locked"
            if na is None:
                err("LOCKED_VISIT_REMOVED", f"{label} visit {vid} disappeared from the plan", visit_id=vid)
                continue
            if sorted(na.employee_ids) != sorted(ra.employee_ids) or na.start != ra.start or na.end != ra.end:
                err(
                    "LOCKED_VISIT_MOVED",
                    f"{label} visit {vid} changed: {sorted(ra.employee_ids)} {_hhmm(ra.start)}-{_hhmm(ra.end)} -> "
                    f"{sorted(na.employee_ids)} {_hhmm(na.start)}-{_hhmm(na.end)}",
                    visit_id=vid,
                )

    return ValidationReport(valid=not errors, errors=errors, warnings=warnings, checks=checks)
