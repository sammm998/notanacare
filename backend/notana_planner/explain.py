"""Deterministic explanations generated from structured facts only.

Nothing here invents information: every sentence is rendered from counts and
values computed from the plans, the validator and the diagnostics.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Any

from . import catalog as C
from .domain import Plan, Scenario, fmt_time

if TYPE_CHECKING:  # pragma: no cover
    from .incidents import IncidentEffect
    from .planner import World
    from .replanning import PhaseResult


def plan_diff(scenario: Scenario, before: Plan, after: Plan, clock: int | None = None) -> dict[str, Any]:
    rows = []
    counts = {
        "visits_changed": 0,
        "reassigned": 0,
        "retimed": 0,
        "reassigned_and_retimed": 0,
        "newly_unplanned": 0,
        "newly_planned": 0,
        "double_staffed_moved": 0,
        "locked_unchanged": 0,
    }
    ids = sorted(set(before.assignments) | set(after.assignments) | set(before.unplanned) | set(after.unplanned))
    for vid in ids:
        v = scenario.visits.get(vid)
        a0, a1 = before.assignments.get(vid), after.assignments.get(vid)
        if a0 and clock is not None and a0.start <= clock:
            counts["locked_unchanged"] += 1
            continue
        if a0 is None and a1 is None:
            continue
        if a0 is None:
            change = "newly_planned"
        elif a1 is None:
            change = "newly_unplanned"
        else:
            emp_changed = sorted(a0.employee_ids) != sorted(a1.employee_ids)
            time_changed = a0.start != a1.start
            if not emp_changed and not time_changed:
                continue
            change = (
                "reassigned_and_retimed" if emp_changed and time_changed else "reassigned" if emp_changed else "retimed"
            )
        counts[change] += 1
        counts["visits_changed"] += 1
        double = bool(v and v.required_employee_count == 2)
        if double and a0 and a1 and a0.start != a1.start:
            counts["double_staffed_moved"] += 1
        rows.append(
            {
                "visit_id": vid,
                "recipient_id": v.recipient_id if v else None,
                "change": change,
                "before_employees": a0.employee_ids if a0 else [],
                "after_employees": a1.employee_ids if a1 else [],
                "before_start": a0.start if a0 else None,
                "after_start": a1.start if a1 else None,
                "shift_minutes": (a1.start - a0.start) if (a0 and a1) else None,
                "double_staffed": double,
                "priority": v.priority if v else None,
                "window": [v.earliest_start, v.latest_start] if v else None,
                "unplanned_reason": (
                    after.unplanned[vid].reasons[0].message if a1 is None and vid in after.unplanned and after.unplanned[vid].reasons else None
                ),
            }
        )
    routes_changed = []
    later_returns = []
    for eid, r1 in after.routes.items():
        r0 = before.routes.get(eid)
        s0 = [(s.visit_id, s.start) for s in r0.stops if s.kind == "visit"] if r0 else []
        s1 = [(s.visit_id, s.start) for s in r1.stops if s.kind == "visit"]
        if s0 != s1:
            routes_changed.append(eid)
        if r0 and r0.route_end is not None and r1.route_end is not None and r1.route_end > r0.route_end:
            later_returns.append({"employee_id": eid, "minutes": r1.route_end - r0.route_end})
    counts["routes_changed"] = len(routes_changed)
    counts["routes_unchanged"] = len(after.routes) - len(routes_changed)
    return {"counts": counts, "rows": rows, "routes_changed": routes_changed, "later_returns": later_returns}


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def replan_summary(
    world: "World",
    before: Plan,
    after: Plan,
    effect: "IncidentEffect",
    diff: dict[str, Any],
    chosen: "PhaseResult",
    phases: list["PhaseResult"],
) -> tuple[str, dict[str, Any]]:
    sc = world.scenario
    c = diff["counts"]
    rows = {r["visit_id"]: r for r in diff["rows"]}
    released = sorted(v for v in effect.released_visits if v in sc.visits)
    rel_rows = [rows[v] for v in released if v in rows]
    rel_reassigned = sum(1 for r in rel_rows if r["change"] in ("reassigned", "reassigned_and_retimed"))
    rel_unplanned = [r for r in rel_rows if r["change"] == "newly_unplanned"]
    rel_unchanged = len(released) - len(rel_rows)
    other_rows = [r for r in diff["rows"] if r["visit_id"] not in effect.released_visits]
    moved_in_window = sum(1 for r in diff["rows"] if r["change"] in ("retimed", "reassigned_and_retimed"))
    doubles = [r for r in diff["rows"] if r["double_staffed"] and r["shift_minutes"]]
    lines = [effect.description]
    if released:
        lines.append(f"{_plural(len(released), 'future visit')} {'was' if len(released) == 1 else 'were'} directly affected.")
        if rel_reassigned:
            lines.append(f"{rel_reassigned} {'was' if rel_reassigned == 1 else 'were'} reassigned without violating hard constraints.")
        if rel_unchanged:
            lines.append(f"{rel_unchanged} could stay with the same employee and time.")
    if moved_in_window:
        lines.append(f"{_plural(moved_in_window, 'visit')} moved within {'its' if moved_in_window == 1 else 'their'} allowed time window{'s' if moved_in_window != 1 else ''}.")
    for r in doubles[:3]:
        lines.append(
            f"Double-staffed visit {r['visit_id']} moved by {abs(r['shift_minutes'])} minutes "
            f"({fmt_time(r['before_start'])} -> {fmt_time(r['after_start'])}), both employees synchronised."
        )
    if len(doubles) > 3:
        lines.append(f"{len(doubles) - 3} more double-staffed visits were moved.")
    knock_on = [r for r in other_rows if r["change"] != "newly_planned"]
    if knock_on:
        lines.append(f"{_plural(len(knock_on), 'other visit')} changed as a knock-on effect.")
    newly = [r for r in diff["rows"] if r["change"] == "newly_planned"]
    if newly:
        lines.append(f"{_plural(len(newly), 'previously unplanned visit')} could now be planned.")
    unresolved = [r for r in diff["rows"] if r["change"] == "newly_unplanned"]
    for r in unresolved[:5]:
        lines.append(f"{r['visit_id']} could not be scheduled: {r['unplanned_reason'] or 'no feasible slot found'}")
    if len(unresolved) > 5:
        lines.append(f"{len(unresolved) - 5} more visits could not be scheduled (see the conflicts panel).")
    if not unresolved and released:
        lines.append("No visit became unplanned.")
    tried = ", ".join(f"phase {p.phase} ({'accepted' if p.accepted else p.reason})" for p in phases)
    lines.append(
        f"Strategy: {chosen.name} over {chosen.employees} employees "
        f"({c['routes_unchanged']} routes untouched); tried {tried}."
    )
    v = after.validation
    lines.append(
        f"Independent validation: {'VALID' if v.get('valid') else 'INVALID'} "
        f"({v.get('error_count', 0)} hard violations, {v.get('warning_count', 0)} warnings)."
    )
    facts = {
        "incident": effect.to_dict(),
        "released_visits": len(released),
        "released_reassigned": rel_reassigned,
        "released_unchanged": rel_unchanged,
        "released_unplanned": len(rel_unplanned),
        "visits_changed": c["visits_changed"],
        "moved_within_window": moved_in_window,
        "double_staffed_moved": len(doubles),
        "knock_on_changes": len(knock_on),
        "newly_planned": len(newly),
        "newly_unplanned": len(unresolved),
        "phase_used": chosen.phase,
        "phase_name": chosen.name,
        "employees_in_repair": chosen.employees,
        "routes_untouched": c["routes_unchanged"],
        "valid": v.get("valid"),
    }
    return "\n".join(lines), facts


def explain_visit(world: "World", plan: Plan, visit_id: str, reference: Plan | None = None) -> dict[str, Any]:
    sc = world.scenario
    tm = world.travel()
    v = sc.visits[visit_id]
    r = sc.recipients[v.recipient_id]
    ivs = [sc.interventions[i] for i in v.intervention_ids]
    a = plan.assignments.get(visit_id)
    out: dict[str, Any] = {
        "visit": {
            "id": v.id,
            "status": "planned" if a else ("cancelled" if v.status.value == "cancelled" else "unplanned"),
            "total_duration": v.total_duration_minutes,
            "window": [v.earliest_start, v.latest_start],
            "window_text": f"{fmt_time(v.earliest_start)}-{fmt_time(v.latest_start)}",
            "preferred_start": v.preferred_start,
            "timing": v.timing.value,
            "priority": v.priority,
            "required_skills": v.required_skills,
            "required_delegations": v.required_delegations,
            "required_employee_count": v.required_employee_count,
            "locked": visit_id in plan.locked_visit_ids,
        },
        "recipient": {
            "id": r.id,
            "name": r.name,
            "address": r.address,
            "zone": r.zone,
            "known_employees": r.known_employee_ids,
            "preferred_employees": r.preferred_employee_ids,
            "avoid_employees": r.avoid_employee_ids,
            "continuity_weight": r.continuity_weight,
        },
        "interventions": [
            {
                "id": i.id,
                "type": i.type,
                "label": C.INTERVENTION_BY_KEY[i.type].label if i.type in C.INTERVENTION_BY_KEY else i.type,
                "duration": i.duration_minutes,
                "timing": i.timing.value,
                "preferred": i.preferred_time,
                "window": [i.earliest_start, i.latest_start],
                "priority": i.priority,
                "delegations": i.required_delegations,
                "skills": i.required_skills,
                "double_staffing": i.requires_double_staffing,
            }
            for i in ivs
        ],
    }
    if a is None:
        u = plan.unplanned.get(visit_id)
        out["unplanned_reasons"] = [
            {"code": x.code, "message": x.message, "details": x.details} for x in (u.reasons if u else [])
        ]
    else:
        staff = []
        for role, eid in enumerate(a.employee_ids):
            e = sc.employees[eid]
            route = plan.routes[eid]
            stops = sorted(route.stops, key=lambda s: s.start)
            idx = next(i for i, s in enumerate(stops) if s.visit_id == visit_id)
            st = stops[idx]
            prev = stops[idx - 1] if idx > 0 else None
            nxt = stops[idx + 1] if idx + 1 < len(stops) else None
            to_next = tm.minutes(st.location_id, nxt.location_id if nxt else (route.end_location_id or e.end_location_id))
            # Why this employee: structured factors
            reasons = []
            have = [s for s in v.required_skills if s in e.skills]
            if role == 0 and v.required_delegations:
                reasons.append(f"holds required delegation(s) {', '.join(v.required_delegations)}")
            if have:
                reasons.append(f"has required skill(s) {', '.join(have)}")
            if eid in r.preferred_employee_ids:
                reasons.append("is a preferred employee of the recipient")
            elif eid in r.known_employee_ids:
                reasons.append("already knows the recipient (continuity)")
            else:
                reasons.append("does not know the recipient (continuity cost accepted)")
            if e.team == r.zone:
                reasons.append(f"belongs to team {e.team} (same area)")
            reasons.append(f"travel from previous stop {st.travel_from_prev} min")
            if reference and visit_id in reference.assignments and eid in reference.assignments[visit_id].employee_ids:
                reasons.append("kept from the previous plan (stability)")
            qualified_on_shift = [
                x.id
                for x in sc.employees.values()
                if x.status.value == "working"
                and x.shift_start <= v.latest_start
                and x.shift_end >= v.earliest_start + v.total_duration_minutes
                and all(s in x.skills for s in v.required_skills)
                and (role != 0 or all(d in x.delegations for d in v.required_delegations))
                and x.id not in r.avoid_employee_ids
            ]
            staff.append(
                {
                    "employee_id": eid,
                    "name": e.name,
                    "role": "lead" if role == 0 else "assistant",
                    "start": a.starts.get(eid, a.start),
                    "end": a.starts.get(eid, a.start) + v.total_duration_minutes,
                    "previous_stop": (prev.visit_id or prev.kind) if prev else "start of shift",
                    "travel_from_previous": st.travel_from_prev,
                    "next_stop": (nxt.visit_id or nxt.kind) if nxt else "end of shift",
                    "travel_to_next": to_next,
                    "knows_recipient": eid in r.known_employee_ids,
                    "preferred": eid in r.preferred_employee_ids,
                    "why": reasons,
                    "qualified_alternatives_on_shift": len(qualified_on_shift) - 1,
                }
            )
        known = sum(1 for s in staff if s["knows_recipient"])
        out["assignment"] = {
            "start": a.start,
            "start_text": fmt_time(a.start),
            "end": a.end,
            "preferred_deviation": a.start - v.preferred_start,
            "continuity_score": round(known / max(1, len(staff)), 2),
            "staff": staff,
        }
    if reference is not None:
        a0 = reference.assignments.get(visit_id)
        moved = {
            "compared_to": reference.id,
            "before_employees": a0.employee_ids if a0 else [],
            "before_start": a0.start if a0 else None,
            "after_employees": a.employee_ids if a else [],
            "after_start": a.start if a else None,
        }
        moved["employee_changed"] = bool(a0 and a and sorted(a0.employee_ids) != sorted(a.employee_ids))
        moved["time_changed_minutes"] = (a.start - a0.start) if (a0 and a) else None
        moved["moved"] = bool(moved["employee_changed"] or moved["time_changed_minutes"]) or (bool(a0) != bool(a))
        out["replanning"] = moved
    return out


def explain_plan_text(plan: Plan) -> str:
    s = plan.score
    v = plan.validation
    lines = [
        f"Plan {plan.id} ({plan.strategy}): {s['planned_visits']}/{s['visits_total']} visits planned, "
        f"{s['unplanned_visits']} unplanned ({s.get('unplanned_high_priority', 0)} high priority).",
        f"Travel {s['travel_minutes']} min ({s['avg_travel_per_visit']} min per visit), "
        f"preferred-time deviation avg {s['preferred_time_deviation_avg']} min, continuity "
        f"{round(100 * s['continuity_known_share'])}% known staff.",
        f"Validation: {'VALID' if v.get('valid') else 'INVALID'} ({v.get('error_count', 0)} hard violations).",
    ]
    if plan.unplanned:
        codes: dict[str, int] = {}
        for u in plan.unplanned.values():
            if u.reasons:
                codes[u.reasons[0].code] = codes.get(u.reasons[0].code, 0) + 1
        lines.append("Unplanned by primary reason: " + ", ".join(f"{k} {n}" for k, n in sorted(codes.items(), key=lambda x: -x[1])))
    return "\n".join(lines)


def median(xs: list[int]) -> float:
    return statistics.median(xs) if xs else 0.0
