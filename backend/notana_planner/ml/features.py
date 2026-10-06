"""Features of a re-planning case, computed before any re-planning is done."""

from __future__ import annotations

from ..domain import EmployeeStatus, Plan, TimingKind
from ..incidents import INCIDENT_KINDS, IncidentEffect
from ..planner import World

FEATURES = (
    [f"kind_{k}" for k in INCIDENT_KINDS]
    + [
        "clock_h",
        "released",
        "released_high_priority",
        "released_double",
        "released_hard_window",
        "released_care_min",
        "affected_employees",
        "unplanned_before",
        "remaining_visits",
        "remaining_care_h",
        "remaining_staff_h",
        "load_ratio",
        "staff_working",
        "zones",
        "max_traffic",
    ]
)


def case_features(world: World, plan: Plan, effect: IncidentEffect) -> dict[str, float]:
    sc = world.scenario
    vis = sc.visits
    clock = effect.clock
    rel = [vis[v] for v in effect.released_visits if v in vis]
    remaining = [v for vid, v in vis.items() if v.latest_start >= clock and v.status.value != "cancelled"]
    care = sum(v.total_duration_minutes * v.required_employee_count for v in remaining)
    staff_min = 0
    working = 0
    for e in sc.employees.values():
        if e.status != EmployeeStatus.WORKING or e.shift_end <= clock:
            continue
        working += 1
        free = e.shift_end - max(clock, e.shift_start)
        free -= sum(max(0, min(iv.end, e.shift_end) - max(iv.start, clock)) for iv in e.unavailable)
        staff_min += max(0, free)
    tr = world.traffic
    f = {f"kind_{k}": float(effect.kind == k) for k in INCIDENT_KINDS}
    f.update(
        clock_h=clock / 60,
        released=len(rel),
        released_high_priority=sum(1 for v in rel if v.priority >= 5),
        released_double=sum(1 for v in rel if v.required_employee_count == 2),
        released_hard_window=sum(1 for v in rel if v.timing == TimingKind.HARD),
        released_care_min=sum(v.total_duration_minutes * v.required_employee_count for v in rel),
        affected_employees=len(effect.affected_employees),
        unplanned_before=len(plan.unplanned),
        remaining_visits=len(remaining),
        remaining_care_h=care / 60,
        remaining_staff_h=staff_min / 60,
        load_ratio=care / max(1, staff_min),
        staff_working=working,
        zones=len(effect.zones),
        max_traffic=max([tr.global_multiplier] + list(tr.zone_multipliers.values()) + list(tr.corridor_multipliers.values())),
    )
    return {k: float(f.get(k, 0.0)) for k in FEATURES}
