"""Build :class:`PlanningProblem` instances from a scenario (+ world state)."""

from __future__ import annotations

from ..config import ObjectiveWeights, SolverSettings
from ..domain import BreakRule, Employee, EmployeeStatus, Scenario, Visit
from ..travel import TravelMatrix
from .problem import PlanningProblem, Task, VehicleSpec, fair_workloads, missing_for_role, task_penalty


def vehicle_for(
    emp: Employee,
    settings: SolverSettings,
    available_from: int | None = None,
    location: str | None = None,
    taken_break_ids: set[int] | None = None,
) -> VehicleSpec | None:
    """Vehicle spec for an employee, honouring sickness and unavailability."""
    if emp.status != EmployeeStatus.WORKING:
        return None
    start = max(emp.shift_start, available_from if available_from is not None else emp.shift_start)
    if emp.available_from is not None:
        start = max(start, emp.available_from)
    end = emp.shift_end + max(0, settings.max_overtime_min)
    breaks: list[BreakRule] = []
    for k, b in enumerate(emp.breaks):
        if taken_break_ids and k in taken_break_ids:
            continue
        earliest = max(b.earliest_start, start)
        latest = max(b.latest_start, earliest)
        breaks.append(BreakRule(b.duration_minutes, earliest, latest, b.kind))
    for iv in sorted(emp.unavailable, key=lambda i: i.start):
        if iv.end <= start:
            continue
        if iv.start <= start:
            start = iv.end
            continue
        if iv.end >= end:
            end = min(end, iv.start)
        else:  # unavailable in the middle of the shift: a fixed "break"
            breaks.append(BreakRule(iv.end - iv.start, iv.start, iv.start, kind="unavailable"))
    if end - start < 15:
        return None
    return VehicleSpec(
        employee_id=emp.id,
        start_location=location or emp.current_location_id or emp.start_location_id,
        start_time=start,
        end_location=emp.end_location_id,
        end_time=end,
        shift_start=emp.shift_start,
        shift_end=emp.shift_end,
        breaks=breaks,
        break_location=emp.start_location_id,
        max_workload=emp.max_workload_minutes,
    )


def make_task(
    scenario: Scenario,
    visit: Visit,
    vehicles: list[VehicleSpec],
    weights: ObjectiveWeights,
) -> Task:
    recipient = scenario.recipients[visit.recipient_id]
    allowed: list[list[str]] = []
    for role in range(visit.required_employee_count):
        allowed.append(
            [
                v.employee_id
                for v in vehicles
                if not missing_for_role(scenario.employees[v.employee_id], visit, recipient, role)
            ]
        )
    return Task(
        visit_id=visit.id,
        recipient_id=visit.recipient_id,
        location_id=visit.location_id,
        duration=visit.total_duration_minutes,
        earliest=visit.earliest_start,
        latest=visit.latest_start,
        preferred=visit.preferred_start,
        roles=visit.required_employee_count,
        priority=visit.priority,
        allowed=allowed,
        penalty=task_penalty(visit, weights),
    )


def build_global_problem(
    scenario: Scenario,
    travel: TravelMatrix,
    weights: ObjectiveWeights,
    settings: SolverSettings,
    visit_ids: list[str] | None = None,
) -> PlanningProblem:
    vehicles = []
    for emp in scenario.employees.values():
        v = vehicle_for(emp, settings)
        if v is not None:
            vehicles.append(v)
    ids = visit_ids if visit_ids is not None else sorted(scenario.visits)
    tasks = [make_task(scenario, scenario.visits[i], vehicles, weights) for i in ids]
    fair_workloads(vehicles, sum(t.duration * t.roles for t in tasks))
    return PlanningProblem(
        scenario=scenario,
        travel=travel,
        weights=weights,
        settings=settings,
        vehicles=vehicles,
        tasks=tasks,
        label="global",
    )
