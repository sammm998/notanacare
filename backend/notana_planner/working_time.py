"""Arbetstidslagen (ATL) limits turned into planning bounds.

Used by the solver builder to shape each employee's day *before* optimising,
so the optimizer can only produce compliant routes:

* ATL 15 §: no more than ``max_continuous_work_min`` (5 h) of work in a row.
  Rests are the meal break and unpaid gaps of at least 30 min (split shift).
  -> meal-break windows are clamped and the latest route end is capped.
* Maximum paid work per day (collective agreement, 10 h).
* ATL 13 §: 11 h dygnsvila. Within one day the shift span must leave 11 h;
  in week planning the next day's shift start caps tonight's end
  (``Employee.latest_end_by_rest``).

The independent validator re-checks all of this from the routes themselves
(``validator.py`` does not import this module).
"""

from __future__ import annotations

from .domain import BreakRule, Employee, WorkingTimeRules

MIN_REST_GAP = 30  # an unpaid gap this long counts as a rest (rast)


def rest_gaps(emp: Employee) -> list[tuple[int, int]]:
    """Unpaid gaps inside the shift that count as rests (split-shift gap ...)."""
    return sorted(
        (iv.start, iv.end)
        for iv in emp.unavailable
        if iv.end - iv.start >= MIN_REST_GAP and emp.shift_start < iv.start and iv.end < emp.shift_end
        and not iv.reason.startswith("sick")
    )


def latest_end(emp: Employee, rules: WorkingTimeRules | None, overtime: int) -> int:
    """Latest time the employee may be back at the office (shift end + approved
    overtime, capped by ATL)."""
    end = emp.shift_end + max(0, overtime)
    if rules is None or not rules.enabled:
        return end
    meals = [b for b in emp.breaks if b.kind == "break"]
    gaps = rest_gaps(emp)
    # Last work stretch: from the end of the last possible rest.
    last_rest_end = max(
        [b.latest_start + b.duration_minutes for b in meals] + [g[1] for g in gaps], default=emp.shift_start
    )
    end = min(end, last_rest_end + rules.max_continuous_work_min)
    # Daily maximum of paid work.
    rest_total = sum(b.duration_minutes for b in meals) + sum(g[1] - g[0] for g in gaps)
    end = min(end, emp.shift_start + rules.max_daily_work_min + rest_total)
    # Dygnsvila: 11 h of rest per 24 h (the shift's own span) and towards tomorrow.
    end = min(end, emp.shift_start + 24 * 60 - rules.min_daily_rest_min)
    if emp.latest_end_by_rest is not None:
        end = min(end, emp.latest_end_by_rest)
    return max(emp.shift_start, end)


def clamp_break(b: BreakRule, emp: Employee, rules: WorkingTimeRules | None, end: int) -> BreakRule:
    """Meal break placed so that neither stretch around it exceeds 5 h."""
    if rules is None or not rules.enabled or b.kind != "break":
        return b
    seg_start = max([emp.shift_start] + [g[1] for g in rest_gaps(emp) if g[1] <= b.latest_start])
    latest = min(b.latest_start, seg_start + rules.max_continuous_work_min)
    earliest = max(b.earliest_start, end - b.duration_minutes - rules.max_continuous_work_min)
    if earliest > latest:  # squeezed by a delay: take the break as soon as possible
        latest = earliest
    return BreakRule(b.duration_minutes, earliest, latest, b.kind)
