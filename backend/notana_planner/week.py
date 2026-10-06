"""Week planning: seven day plans for the same recipients and staff.

Days are optimised in order (Monday first). Each day is a normal day problem;
what links the days is:

* the roster (who works which day; two consecutive days off),
* dygnsvila: tonight's latest end is capped by tomorrow's shift start minus
  11 h (``Employee.latest_end_by_rest``, set by the generator),
* continuity: an employee who served a recipient on an earlier day counts as
  "known" on the following days.

``validate_week`` checks the rules that span days from the plans themselves:
11 h dygnsvila between consecutive work days (ATL 13 §), 36 h veckovila in
the 7-day period (ATL 14 §) and weekly hours against contract + overtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .domain import WEEKDAYS, Plan, Scenario
from .planner import World, plan_day

DAY = 24 * 60


@dataclass(slots=True)
class WeekIssue:
    code: str
    message: str
    employee_id: str
    day: int | None = None

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "employee_id": self.employee_id, "day": self.day}


@dataclass(slots=True)
class WeekReport:
    errors: list[WeekIssue] = field(default_factory=list)
    checks: int = 0

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"valid": self.valid, "errors": [e.to_dict() for e in self.errors], "checks": self.checks}


def _work_interval(plan: Plan, eid: str) -> tuple[int, int, int] | None:
    """(start, end, worked minutes) of an employee's day, or None if no work."""
    r = plan.routes.get(eid)
    if r is None or not r.stops or r.route_start is None:
        return None
    end = r.route_end if r.route_end is not None else max(s.end for s in r.stops)
    rests = sum(s.end - s.start for s in r.stops if s.kind in ("break", "unavailable") and s.end - s.start >= 30)
    return r.route_start, end, max(0, end - r.route_start - rests)


def validate_week(scenarios: list[Scenario], plans: list[Plan | None]) -> WeekReport:
    rep = WeekReport()
    rules = scenarios[0].rules
    if not rules.enabled:
        return rep
    staff: dict[str, Any] = {}
    for sc in scenarios:
        for eid, e in sc.employees.items():
            staff.setdefault(eid, e)
    for eid, e in sorted(staff.items()):
        work: list[tuple[int, int, int, int]] = []  # (day, abs start, abs end, worked)
        for d, (sc, plan) in enumerate(zip(scenarios, plans)):
            if plan is None or eid not in sc.employees:
                continue
            iv = _work_interval(plan, eid)
            if iv is not None:
                work.append((d, d * DAY + iv[0], d * DAY + iv[1], iv[2]))
        # Dygnsvila between consecutive work periods
        for (d1, _, e1, _), (d2, s2, _, _) in zip(work, work[1:]):
            rep.checks += 1
            if s2 - e1 < rules.min_daily_rest_min:
                rep.errors.append(WeekIssue(
                    "DAILY_REST_VIOLATED",
                    f"{eid}: only {(s2 - e1) / 60:.1f} h rest between {WEEKDAYS[d1]} and {WEEKDAYS[d2]} "
                    f"(min {rules.min_daily_rest_min // 60} h, ATL 13 §)",
                    eid, d2))
        # Veckovila: longest continuous rest in the 7-day period. The roster repeats
        # every week, so the rest after Sunday continues into Monday.
        if len(plans) >= 7 and all(p is not None for p in plans):
            rep.checks += 1
            if work:
                gaps = [s2 - e1 for (_, _, e1, _), (_, s2, _, _) in zip(work, work[1:])]
                gaps.append(7 * DAY - work[-1][2] + work[0][1])
                longest = max(gaps)
            else:
                longest = 7 * DAY
            if longest < rules.min_weekly_rest_min:
                rep.errors.append(WeekIssue(
                    "WEEKLY_REST_VIOLATED",
                    f"{eid}: longest rest in the week is {longest / 60:.1f} h (min {rules.min_weekly_rest_min // 60} h, ATL 14 §)",
                    eid))
            # Weekly hours
            rep.checks += 1
            worked = sum(w for *_, w in work)
            cap = e.contract_minutes_per_week + rules.max_weekly_overtime_min
            if worked > cap:
                rep.errors.append(WeekIssue(
                    "WEEKLY_HOURS_EXCEEDED",
                    f"{eid}: {worked / 60:.1f} h this week, contract {e.contract_minutes_per_week / 60:.1f} h "
                    f"+ max {rules.max_weekly_overtime_min / 60:.1f} h overtime",
                    eid))
    return rep


def plan_week(
    worlds: list[World],
    strategy: str = "baseline",
    time_limit_s: float | None = None,
    progress: Callable[[str], None] | None = None,
    plan_fn: Callable[[World, str, float | None], Plan] = plan_day,
) -> list[Plan]:
    plans: list[Plan] = []
    for d, w in enumerate(worlds):
        if progress:
            progress(f"{WEEKDAYS[w.scenario.day]}: optimising {len(w.scenario.visits)} visits "
                     f"with {len(w.scenario.employees)} employees on duty")
        plan = plan_fn(w, strategy, time_limit_s)
        plans.append(plan)
        later = [x.scenario for x in worlds[d + 1:]]
        recips = {vid: w.scenario.visits[vid].recipient_id for vid in plan.assignments if vid in w.scenario.visits}
        for vid, a in plan.assignments.items():
            rid = recips.get(vid)
            for sc in later:
                r = sc.recipients.get(rid) if rid else None
                if r is None:
                    continue
                for eid in a.employee_ids:
                    if eid not in r.known_employee_ids:
                        r.known_employee_ids.append(eid)
    return plans


def week_summary(scenarios: list[Scenario], plans: list[Plan | None]) -> dict:
    days = []
    for sc, p in zip(scenarios, plans):
        s = p.score if p is not None else {}
        days.append({
            "day": sc.day,
            "weekday": sc.weekday,
            "scenario_id": sc.id,
            "plan_id": p.id if p is not None else None,
            "visits": len(sc.visits),
            "interventions": len(sc.interventions),
            "employees_on_duty": len(sc.employees),
            "planned": len(p.assignments) if p is not None else None,
            "unplanned": len(p.unplanned) if p is not None else None,
            "valid": p.validation.get("valid") if p is not None else None,
            "travel_minutes": s.get("travel_minutes"),
            "continuity_known_share": s.get("continuity_known_share"),
        })
    staff: dict[str, dict] = {}
    for d, (sc, p) in enumerate(zip(scenarios, plans)):
        for eid, e in sc.employees.items():
            row = staff.setdefault(eid, {
                "employee_id": eid, "name": e.name, "team": e.team, "shift": e.shift_name,
                "contract_hours": round(e.contract_minutes_per_week / 60, 1),
                "days_off": [WEEKDAYS[x] for x in e.days_off], "hours": [None] * 7, "worked_hours": 0.0,
            })
            if p is None:
                continue
            iv = _work_interval(p, eid)
            h = round(iv[2] / 60, 2) if iv else 0.0
            row["hours"][d] = h
            row["worked_hours"] = round(row["worked_hours"] + h, 2)
    rep = validate_week(scenarios, plans)
    planned = [d for d in days if d["planned"] is not None]
    return {
        "days": days,
        "totals": {
            "interventions": sum(d["interventions"] for d in days),
            "visits": sum(d["visits"] for d in days),
            "planned": sum(d["planned"] for d in planned) if planned else None,
            "unplanned": sum(d["unplanned"] for d in planned) if planned else None,
            "headcount": len(staff),
            "days_planned": len(planned),
        },
        "employees": sorted(staff.values(), key=lambda r: r["employee_id"]),
        "validation": rep.to_dict(),
        "rules": {
            "min_daily_rest_h": scenarios[0].rules.min_daily_rest_min / 60,
            "min_weekly_rest_h": scenarios[0].rules.min_weekly_rest_min / 60,
            "max_continuous_work_h": scenarios[0].rules.max_continuous_work_min / 60,
            "max_daily_work_h": scenarios[0].rules.max_daily_work_min / 60,
            "max_weekly_overtime_h": scenarios[0].rules.max_weekly_overtime_min / 60,
        },
    }
