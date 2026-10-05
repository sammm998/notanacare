"""Solver-facing problem definition.

A :class:`PlanningProblem` is the exact sub-problem handed to the optimizer:
which employees ("vehicles") take part, from where/when they start, which
visits ("tasks") are free to be (re)planned, and which are pinned. Global
planning and every local-repair neighbourhood are expressed the same way.

Double-staffed visits are a single task with two *roles*: role 0 (lead) must
hold all required skills **and** delegations, role 1 (assistant) all required
skills. The two roles are synchronised in time and must be different people.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ObjectiveWeights, SolverSettings
from ..domain import BreakRule, CareRecipient, Employee, Scenario, Visit
from ..travel import TravelMatrix


@dataclass(slots=True)
class VehicleSpec:
    employee_id: str
    start_location: str
    start_time: int  # earliest departure / availability
    end_location: str
    end_time: int  # hard latest arrival at end location (shift end + approved overtime)
    shift_start: int
    shift_end: int
    breaks: list[BreakRule] = field(default_factory=list)
    break_location: str = ""  # breaks are taken at the team office
    fair_workload: int = 0
    max_workload: int | None = None


@dataclass(slots=True)
class Task:
    visit_id: str
    recipient_id: str
    location_id: str
    duration: int
    earliest: int
    latest: int
    preferred: int
    roles: int  # 1 or 2
    priority: int
    allowed: list[list[str]]  # per role: employee ids allowed in this problem
    penalty: int  # per role, cost of leaving the task unplanned
    previous_employees: list[str] = field(default_factory=list)  # stability reference
    previous_start: int | None = None  # previously communicated start
    pinned: dict[int, tuple[str, int]] | None = None  # role -> (employee, start)


@dataclass(slots=True)
class PlanningProblem:
    scenario: Scenario
    travel: TravelMatrix
    weights: ObjectiveWeights
    settings: SolverSettings
    vehicles: list[VehicleSpec]
    tasks: list[Task]
    # Fixed busy intervals of recipients from visits outside this problem.
    recipient_busy: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # Optional warm start: employee -> ordered list of (visit_id, role, start).
    hint_routes: dict[str, list[tuple[str, int, int]]] | None = None
    # Employees whose routes were not affected by the incident (stability).
    unaffected_employees: set[str] = field(default_factory=set)
    label: str = "global"

    def vehicle_index(self) -> dict[str, int]:
        return {v.employee_id: i for i, v in enumerate(self.vehicles)}


# ---------------------------------------------------------------------------
# Qualification helpers (shared with the validator's *own* re-implementation
# deliberately NOT shared: the validator re-checks everything independently).
# ---------------------------------------------------------------------------


def missing_for_role(emp: Employee, visit: Visit, recipient: CareRecipient, role: int) -> list[str]:
    missing = [f"skill:{s}" for s in visit.required_skills if s not in emp.skills]
    if role == 0:
        missing += [f"delegation:{d}" for d in visit.required_delegations if d not in emp.delegations]
    if emp.id in recipient.avoid_employee_ids:
        missing.append("recipient-avoids-employee")
    return missing


def task_penalty(visit: Visit, weights: ObjectiveWeights) -> int:
    return int(weights.unplanned_visit * (1 + weights.priority_multiplier * (visit.priority - 1)))


def fair_workloads(vehicles: list[VehicleSpec], total_service: int) -> None:
    cap = sum(max(0, v.end_time - v.start_time) for v in vehicles) or 1
    for v in vehicles:
        v.fair_workload = int(total_service * max(0, v.end_time - v.start_time) / cap)
