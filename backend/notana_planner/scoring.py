"""Objective score breakdown, recomputed from the plan (not from solver internals).

Every KPI shown in the UI comes from here. The weighted breakdown uses the same
weights as the optimizer, so the user can see *why* plan A scores better than
plan B, term by term.
"""

from __future__ import annotations

import statistics
from typing import Any

from .config import ObjectiveWeights
from .domain import Plan, Scenario
from .travel import TravelMatrix


def score_plan(
    scenario: Scenario,
    travel: TravelMatrix,
    plan: Plan,
    weights: ObjectiveWeights,
    reference: Plan | None = None,
) -> dict[str, Any]:
    visits = scenario.visits
    emps = scenario.employees
    planned = [vid for vid in plan.assignments if vid in visits]
    unplanned = [vid for vid in plan.unplanned if vid in visits]
    unplanned_by_priority: dict[int, int] = {}
    for vid in unplanned:
        p = visits[vid].priority
        unplanned_by_priority[p] = unplanned_by_priority.get(p, 0) + 1
    unplanned_slots = sum(visits[v].required_employee_count for v in unplanned)
    unplanned_penalty = sum(
        visits[v].required_employee_count
        * weights.unplanned_visit
        * (1 + weights.priority_multiplier * (visits[v].priority - 1))
        for v in unplanned
    )

    travel_min = 0
    travel_km = 0.0
    care_by_emp: dict[str, int] = {}
    idle_min = 0
    overtime = 0
    span_min = 0
    working = 0
    for eid, r in plan.routes.items():
        if eid not in emps:
            continue
        emp = emps[eid]
        stops = sorted(r.stops, key=lambda s: s.start)
        vstops = [s for s in stops if s.kind == "visit"]
        if not vstops:
            continue
        working += 1
        loc = r.start_location_id or emp.start_location_id
        t_prev_end = r.route_start if r.route_start is not None else stops[0].start
        for s in stops:
            t = travel.minutes(loc, s.location_id)
            travel_min += t
            travel_km += travel.km(loc, s.location_id)
            idle_min += max(0, s.start - t_prev_end - t)
            loc = s.location_id
            t_prev_end = s.end
        travel_min += travel.minutes(loc, r.end_location_id or emp.end_location_id)
        travel_km += travel.km(loc, r.end_location_id or emp.end_location_id)
        care_by_emp[eid] = sum(s.end - s.start for s in vstops)
        if r.route_end is not None:
            overtime += max(0, r.route_end - emp.shift_end)
            if r.route_start is not None:
                span_min += r.route_end - r.route_start

    # Preferred-time deviation (per visit, lead start).
    devs = []
    for vid in planned:
        a = plan.assignments[vid]
        devs.append(abs(a.start - visits[vid].preferred_start))
    pref_dev_total = sum(devs)

    # Continuity
    roles_total = 0
    roles_known = 0
    roles_preferred = 0
    recip_with_pref = 0
    continuity_pen = 0.0
    distinct_staff: dict[str, set[str]] = {}
    for vid in planned:
        v = visits[vid]
        r = scenario.recipients[v.recipient_id]
        for eid in plan.assignments[vid].employee_ids:
            roles_total += 1
            distinct_staff.setdefault(r.id, set()).add(eid)
            if eid in r.known_employee_ids:
                roles_known += 1
            else:
                continuity_pen += weights.continuity_unknown_employee * r.continuity_weight
            if r.preferred_employee_ids:
                recip_with_pref += 1
                if eid in r.preferred_employee_ids:
                    roles_preferred += 1
                else:
                    continuity_pen += weights.continuity_preferred_bonus * r.continuity_weight
    staff_per_recipient = (
        statistics.mean(len(s) for s in distinct_staff.values()) if distinct_staff else 0.0
    )

    # Workload balance (care minutes of employees that work at all)
    loads = list(care_by_emp.values())
    load_std = statistics.pstdev(loads) if len(loads) > 1 else 0.0
    shift_minutes = sum(
        max(0, e.shift_end - e.shift_start) - sum(i.end - i.start for i in e.unavailable)
        for e in emps.values()
        if e.status.value == "working"
    )
    care_total = sum(loads)

    # Stability vs reference
    stability = {"compared_to": None}
    if reference is not None:
        emp_changes = 0
        time_changes = 0
        time_change_minutes = 0
        newly_unplanned = 0
        newly_planned = 0
        changed_visits = set()
        for vid, ra in reference.assignments.items():
            na = plan.assignments.get(vid)
            if na is None:
                if vid in plan.unplanned:
                    newly_unplanned += 1
                    changed_visits.add(vid)
                continue
            if sorted(na.employee_ids) != sorted(ra.employee_ids):
                emp_changes += 1
                changed_visits.add(vid)
            if na.start != ra.start:
                time_changes += 1
                time_change_minutes += abs(na.start - ra.start)
                changed_visits.add(vid)
        for vid in plan.assignments:
            if vid not in reference.assignments:
                newly_planned += 1
                changed_visits.add(vid)
        route_changes = 0
        for eid, r in plan.routes.items():
            old = reference.routes.get(eid)
            seq_new = [s.visit_id for s in r.stops if s.kind == "visit"]
            seq_old = [s.visit_id for s in old.stops if s.kind == "visit"] if old else []
            if seq_new != seq_old:
                route_changes += 1
        stability = {
            "compared_to": reference.id,
            "visits_changed": len(changed_visits),
            "employee_changes": emp_changes,
            "start_time_changes": time_changes,
            "start_time_change_minutes": time_change_minutes,
            "routes_changed": route_changes,
            "newly_unplanned": newly_unplanned,
            "newly_planned": newly_planned,
        }

    weighted = {
        "unplanned_visits": round(unplanned_penalty),
        "travel_time": weights.travel_minute * travel_min,
        "travel_distance": round(weights.travel_km * travel_km),
        "preferred_time_deviation": weights.preferred_time_minute * pref_dev_total,
        "continuity": round(continuity_pen),
        "overtime": weights.overtime_minute * overtime,
        "idle_span": weights.idle_minute * span_min,
    }
    if reference is not None:
        weighted["employee_changes"] = weights.employee_change * stability["employee_changes"]
        weighted["visit_time_changes"] = weights.time_change_minute * stability["start_time_change_minutes"]
    weighted["total"] = sum(v for k, v in weighted.items())

    return {
        "visits_total": len(visits),
        "planned_visits": len(planned),
        "unplanned_visits": len(unplanned),
        "unplanned_employee_slots": unplanned_slots,
        "unplanned_by_priority": {str(k): v for k, v in sorted(unplanned_by_priority.items())},
        "unplanned_high_priority": sum(v for k, v in unplanned_by_priority.items() if k >= 5),
        "planned_share": round(len(planned) / max(1, len(visits)), 4),
        "hard_violations": len(plan.validation.get("errors", [])) if plan.validation else None,
        "travel_minutes": travel_min,
        "travel_km": round(travel_km, 1),
        "avg_travel_per_visit": round(travel_min / max(1, len(planned)), 1),
        "preferred_time_deviation_total": pref_dev_total,
        "preferred_time_deviation_avg": round(pref_dev_total / max(1, len(planned)), 1),
        "preferred_time_deviation_max": max(devs) if devs else 0,
        "continuity_known_share": round(roles_known / max(1, roles_total), 3),
        "continuity_preferred_share": round(roles_preferred / max(1, recip_with_pref), 3) if recip_with_pref else None,
        "staff_per_recipient": round(staff_per_recipient, 2),
        "overtime_minutes": overtime,
        "idle_minutes": idle_min,
        "care_minutes": care_total,
        "utilization": round(care_total / max(1, shift_minutes), 3),
        "workload_std_minutes": round(load_std, 1),
        "workload_min_minutes": min(loads) if loads else 0,
        "workload_max_minutes": max(loads) if loads else 0,
        "employees_working": working,
        "stability": stability,
        "weighted": weighted,
        "solver_runtime_s": plan.solver_stats.get("seconds_total"),
    }
