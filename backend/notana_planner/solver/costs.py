"""Soft-cost terms shared by construction, routing and timetabling."""

from __future__ import annotations

from .problem import PlanningProblem, Task


def assignment_penalty(problem: PlanningProblem, task: Task, employee_id: str) -> int:
    """Cost of letting ``employee_id`` serve one role of ``task``.

    Continuity (does the employee know the recipient / are they preferred)
    plus re-planning stability (was the visit previously with someone else).
    """
    w = problem.weights
    r = problem.scenario.recipients[task.recipient_id]
    cw = r.continuity_weight
    pen = 0.0
    if employee_id not in r.known_employee_ids:
        pen += w.continuity_unknown_employee * cw
    if r.preferred_employee_ids and employee_id not in r.preferred_employee_ids:
        pen += w.continuity_preferred_bonus * cw
    # Wishes (strict variants are hard and handled by allowed vehicles)
    emp = problem.scenario.employees[employee_id]
    if r.gender_preference and not r.gender_strict and emp.gender != r.gender_preference:
        if r.gender_scope == "all" or problem.scenario.visits[task.visit_id].intimate_care:
            pen += w.gender_wish_mismatch
    if r.languages and not set(r.languages) & set(emp.languages):
        pen += w.language_wish_mismatch
    if emp.preferred_zones and r.zone not in emp.preferred_zones:
        pen += w.outside_preferred_zone
    if w.outside_team_zone and r.zone != emp.team:
        pen += w.outside_team_zone
    if task.previous_employees and employee_id not in task.previous_employees:
        pen += w.employee_change
        if any(e in problem.unaffected_employees for e in task.previous_employees):
            pen += w.unaffected_route_touch
    return int(pen)


def time_target(task: Task) -> int:
    """Reference start: previously communicated time if any, else preferred."""
    return task.previous_start if task.previous_start is not None else task.preferred


def time_deviation_cost(problem: PlanningProblem, task: Task, start: int) -> int:
    w = problem.weights
    cost = w.preferred_time_minute * abs(start - task.preferred)
    if task.previous_start is not None:
        cost += w.time_change_minute * abs(start - task.previous_start)
    return cost


def clamp(x: int, lo: int, hi: int) -> int:
    return lo if x < lo else hi if x > hi else x
