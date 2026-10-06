"""Intervention-level view of a plan: every single intervention, who does it, or why not.

The optimizer plans *visits*; people think in *interventions* (≈5 000 a day).
This module maps each intervention to its visit and that visit's assignment,
and summarises the plan in the terms a unit manager uses.
"""

from __future__ import annotations

from typing import Any

from . import catalog as C
from .domain import EmployeeStatus, Plan, TimingKind, VisitStatus, fmt_time
from .planner import World

FILTERS = {
    "all": "Alla insatser",
    "assigned": "Tilldelade",
    "unassigned": "Utan tilldelning",
    "critical": "Tidskritiska",
    "double": "Dubbelbemanning",
    "requirements": "Med krav (delegering / kompetens)",
}


def rows(world: World, plan: Plan | None) -> list[dict[str, Any]]:
    sc = world.scenario
    emps = sc.employees
    out = []
    for v in sorted(sc.visits.values(), key=lambda v: (v.earliest_start, v.id)):
        a = plan.assignments.get(v.id) if plan else None
        u = plan.unplanned.get(v.id) if plan else None
        r = sc.recipients[v.recipient_id]
        cancelled = v.status == VisitStatus.CANCELLED
        staff = [{"id": e, "name": emps[e].name.split(" (")[0] if e in emps else e} for e in (a.employee_ids if a else [])]
        reason = None
        if u and u.reasons:
            reason = {"code": u.reasons[0].code, "message": u.reasons[0].message}
        for iid in v.intervention_ids:
            it = sc.interventions.get(iid)
            if it is None:
                continue
            t = C.INTERVENTION_BY_KEY.get(it.type)
            reqs = list(it.required_delegations) + list(it.required_skills)
            out.append({
                "id": iid,
                "visit_id": v.id,
                "recipient_id": r.id,
                "recipient": r.name,
                "zone": r.zone,
                "need": t.label if t else it.type,
                "type": it.type,
                "window": f"{fmt_time(it.earliest_start)}–{fmt_time(it.latest_start)}",
                "earliest": it.earliest_start,
                "duration": it.duration_minutes,
                "timing": "Tidskritisk" if it.timing == TimingKind.HARD else "Tidsönskemål",
                "critical": it.timing == TimingKind.HARD,
                "priority": it.priority,
                "requirements": reqs,
                "double": it.requires_double_staffing or v.required_employee_count == 2,
                "status": "cancelled" if cancelled else ("assigned" if a else "unassigned"),
                "start": fmt_time(a.start) if a else None,
                "staff": staff,
                "reason": reason,
            })
    return out


def _match(row: dict, q: str) -> bool:
    hay = " ".join([row["id"], row["visit_id"], row["recipient_id"], row["recipient"], row["need"], row["zone"],
                    " ".join(row["requirements"]) or "grundkompetens",
                    " ".join(f"{s['id']} {s['name']}" for s in row["staff"])]).lower()
    return all(term in hay for term in q.lower().split())


def query(all_rows: list[dict], q: str = "", flt: str = "all", offset: int = 0, limit: int = 100) -> dict:
    sel = all_rows
    if flt == "assigned":
        sel = [r for r in sel if r["status"] == "assigned"]
    elif flt == "unassigned":
        sel = [r for r in sel if r["status"] == "unassigned"]
    elif flt == "critical":
        sel = [r for r in sel if r["critical"]]
    elif flt == "double":
        sel = [r for r in sel if r["double"]]
    elif flt == "requirements":
        sel = [r for r in sel if r["requirements"]]
    if q.strip():
        sel = [r for r in sel if _match(r, q)]
    return {"total": len(sel), "offset": offset, "limit": limit, "rows": sel[offset:offset + limit]}


def summary(world: World, plan: Plan | None, all_rows: list[dict]) -> dict:
    sc = world.scenario
    active = [r for r in all_rows if r["status"] != "cancelled"]
    assigned = sum(1 for r in active if r["status"] == "assigned")
    visits_active = [v for v in sc.visits.values() if v.status != VisitStatus.CANCELLED]
    out: dict[str, Any] = {
        "interventions_total": len(active),
        "interventions_assigned": assigned,
        "interventions_unassigned": len(active) - assigned,
        "visits_total": len(visits_active),
        "recipients": len(sc.recipients),
        "employees": len(sc.employees),
        "filters": FILTERS,
        "area": sc.area_name,
        "weekday": sc.weekday,
        "seed": sc.config.seed,
    }
    if plan is None:
        return out
    working = [e for e in sc.employees.values() if e.status == EmployeeStatus.WORKING]
    bad = {e["employee_id"] for e in plan.validation.get("errors", []) if e.get("employee_id")}
    rr = plan.solver_stats.get("ruin_recreate", {})
    out.update(
        plan_id=plan.id,
        visits_planned=len(plan.assignments),
        visits_unplanned=len(plan.unplanned),
        unplanned_priority_weight=sum(sc.visits[v].priority for v in plan.unplanned if v in sc.visits),
        unplanned_high_priority=sum(1 for v in plan.unplanned if v in sc.visits and sc.visits[v].priority >= 5),
        staff_compliant=sum(1 for e in working if e.id not in bad),
        staff_working=len(working),
        continuity=plan.score.get("continuity_known_share"),
        runtime_s=plan.solver_stats.get("seconds_total"),
        iterations=rr.get("iterations"),
        valid=plan.validation.get("valid"),
        status=plan.validation.get("status"),
        approved_exceptions=len(plan.validation.get("approved_exceptions", [])),
        rules_checked=["Arbetstidslagen (5 h utan rast, 10 h/dag, 11 h dygnsvila)", "kompetens och delegering",
                       "önskemål (kön, språk, allergi, rökning)", "tidsfönster", "dubbelbemanning"],
    )
    return out
