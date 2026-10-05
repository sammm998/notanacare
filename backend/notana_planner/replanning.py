"""Real-time re-planning with stability and phased local repair.

Given the current plan, the simulation clock and an incident:

* Everything that has started (completed or ongoing visits, taken breaks) is
  frozen and copied verbatim (``locked``).
* **Phase 1 - local repair**: affected employees + a few nearby qualified
  helpers, affected visits + the helpers' visits within a short horizon.
  The helpers' later visits are pinned, all other routes are untouched.
* **Phase 2 - expanded neighbourhood**: more helpers, rest of the day.
* **Phase 3 - broad re-optimisation**: all employees, all remaining visits,
  warm-started from the current plan with stability penalties.

A phase is *accepted* when no visit that was planned (or released by the
incident) ends up unplanned and the plan validates. Otherwise the next phase
runs. Stability penalties (employee change, start-time change, touching an
unaffected route) apply in every phase, so even phase 3 does not rewrite the
day unless it must.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any

from .domain import Plan, RouteStop, VisitStatus, fmt_time
from .incidents import IncidentEffect
from .plan_builder import assemble_plan
from .planner import World, finalize_plan
from .solver.builder import make_task, vehicle_for
from .solver.engine import SolveEngine, SolveOptions
from .solver.problem import PlanningProblem, Task, VehicleSpec, fair_workloads


@dataclass(slots=True)
class RepairParams:
    phase1_helpers: int = 6
    phase1_horizon_min: int = 180
    phase2_helpers: int = 24
    phase1_time_s: float = 5.0
    phase2_time_s: float = 10.0
    phase3_time_s: float = 20.0
    start_phase: int = 1
    max_phase: int = 3
    enhanced_repair: bool = False
    source: str = "default"

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}  # type: ignore[attr-defined]


@dataclass(slots=True)
class PhaseResult:
    phase: int
    name: str
    employees: int
    free_visits: int
    pinned_visits: int
    accepted: bool
    plan: Plan
    seconds: float
    reason: str = ""
    lost: list[str] = field(default_factory=list)  # communicated / released visits now unplanned


@dataclass
class ReplanResult:
    plan: Plan
    phases: list[PhaseResult]
    strategy_used: str
    effect: IncidentEffect
    diff: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    summary_facts: dict[str, Any] = field(default_factory=dict)


def frozen_prefixes(plan: Plan, clock: int) -> dict[str, list[RouteStop]]:
    """Stops that have started by ``clock`` (visits, breaks, gaps)."""
    out: dict[str, list[RouteStop]] = {}
    started_visits = {vid for vid, a in plan.assignments.items() if a.start <= clock}
    for eid, r in plan.routes.items():
        pre = [s for s in r.stops if s.start <= clock or (s.visit_id in started_visits)]
        if pre:
            out[eid] = pre
    return out


def _employee_state(world: World, plan: Plan, eid: str, prefix: list[RouteStop], clock: int) -> VehicleSpec | None:
    emp = world.scenario.employees[eid]
    loc = None
    ready = clock
    if prefix:
        last = max(prefix, key=lambda s: s.end)
        loc = last.location_id
        ready = max(clock, last.end)
    if emp.current_location_id and emp.available_from is not None:
        loc = emp.current_location_id
    n_taken = sum(1 for s in prefix if s.kind == "break")
    v = vehicle_for(
        emp,
        world.settings,
        available_from=ready,
        location=loc,
        taken_break_ids=set(range(n_taken)),
    )
    return v


def _neighbourhood(
    world: World, plan: Plan, effect: IncidentEffect, clock: int, n_helpers: int, released: set[str]
) -> set[str]:
    """Affected employees + the ``n_helpers`` most useful nearby helpers.

    Helpers are ranked by how many released visits they could absorb in an
    existing opening of their current route (exact gap check incl. travel),
    then by team / zone match and travel distance.
    """
    from .diagnostics import _gaps

    sc = world.scenario
    tm = world.travel()
    core = {e for e in effect.affected_employees if e in sc.employees}
    target_visits = [sc.visits[v] for v in released if v in sc.visits]
    if not target_visits or n_helpers <= 0:
        return core
    zones = {sc.recipients[v.recipient_id].zone for v in target_visits} | effect.zones
    scored = []
    for eid, emp in sc.employees.items():
        if eid in core or emp.status.value != "working" or emp.shift_end <= clock:
            continue
        r = plan.routes.get(eid)
        locs = [s.location_id for s in (r.stops if r else []) if s.start > clock] or [emp.start_location_id]
        can = 0
        openings = 0
        dist = 10**6
        for v in target_visits:
            if emp.shift_start > v.latest_start or emp.shift_end < v.earliest_start + v.total_duration_minutes:
                continue
            if any(s not in emp.skills for s in v.required_skills):
                continue
            can += 1
            dist = min(dist, min(tm.minutes(l, v.location_id) for l in locs))
            if any(g.shortfall == 0 and g.lo >= clock for g in _gaps(sc, tm, plan, eid, v, False)):
                openings += 1
        if not can:
            continue
        scored.append((-openings, -(emp.team in zones), dist, -can, eid))
    scored.sort()
    return core | {eid for *_, eid in scored[:n_helpers]}


def build_repair_problem(
    world: World,
    plan: Plan,
    effect: IncidentEffect,
    clock: int,
    employees: set[str] | None,
    horizon_end: int | None,
    label: str,
) -> tuple[PlanningProblem, dict[str, list[RouteStop]], set[str]]:
    """Problem over ``employees`` (None = everyone). Returns (problem, frozen, kept-routes)."""
    sc = world.scenario
    tm = world.travel()
    prefixes = frozen_prefixes(plan, clock)
    started = {vid for vid, a in plan.assignments.items() if a.start <= clock}
    in_problem = set(sc.employees) if employees is None else set(employees)
    vehicles: list[VehicleSpec] = []
    for eid in sorted(in_problem):
        v = _employee_state(world, plan, eid, prefixes.get(eid, []), clock)
        if v is not None:
            vehicles.append(v)
    vehicle_ids = {v.employee_id for v in vehicles}
    keep_routes = set(sc.employees) - in_problem

    released = set(effect.released_visits)
    tasks: list[Task] = []
    task_ids: set[str] = set()

    def add_task(vid: str, pinned_roles: dict[int, tuple[str, int]] | None = None, roles_here: list[int] | None = None) -> None:
        v = sc.visits[vid]
        t = make_task(sc, v, vehicles, world.weights)
        a = plan.assignments.get(vid)
        if a is not None and vid not in effect.changed_visits:
            t.previous_employees = list(a.employee_ids)
            t.previous_start = a.start
        elif a is not None:
            t.previous_employees = list(a.employee_ids)
        if a is not None:
            # Un-planning a visit that is already communicated costs extra.
            t.penalty += world.weights.drop_communicated_visit
        if roles_here is not None and len(roles_here) < v.required_employee_count:
            # Partial double visit: only some roles are served inside this problem.
            t.roles = len(roles_here)
            t.allowed = [t.allowed[r] if r < len(t.allowed) else [] for r in roles_here]
        if pinned_roles:
            t.pinned = pinned_roles
        tasks.append(t)
        task_ids.add(vid)

    for vid, v in sc.visits.items():
        if vid in started or v.status == VisitStatus.CANCELLED:
            continue
        if v.latest_start < clock:
            continue  # window already passed
        a = plan.assignments.get(vid)
        emps = list(a.employee_ids) if a else []
        inside = [e for e in emps if e in in_problem]
        if a is None:
            # Previously unplanned (or new) visit: try it if the incident may help.
            if vid in released or effect.widen_to_unplanned or employees is None:
                add_task(vid)
            continue
        if not inside and vid not in released:
            continue  # untouched route keeps it
        free = vid in released or (
            all(e in in_problem for e in emps) and (horizon_end is None or a.start < horizon_end)
        )
        if any(e in effect.affected_employees for e in emps) and all(e in in_problem for e in emps):
            free = True
        if v.locked and all(e in vehicle_ids for e in emps):
            free = False  # user-locked: same employees, same time
        if free and all(e in in_problem or e not in vehicle_ids for e in emps):
            add_task(vid)
        else:
            # Pinned in place for the employees inside this problem.
            roles = [i for i, e in enumerate(emps) if e in vehicle_ids]
            if not roles:
                continue
            pinned = {k: (emps[r], a.starts.get(emps[r], a.start)) for k, r in enumerate(roles)}
            add_task(vid, pinned_roles=pinned, roles_here=roles)

    # Recipient busy intervals from everything that stays outside the problem.
    busy: dict[str, list[tuple[int, int]]] = {}
    for vid, a in plan.assignments.items():
        if vid in task_ids or vid not in sc.visits:
            continue
        rid = sc.visits[vid].recipient_id
        busy.setdefault(rid, []).append((a.start, a.end))

    # Warm start: previous future sequence of every vehicle.
    hint: dict[str, list[tuple[str, int, int]]] = {}
    for v in vehicles:
        r = plan.routes.get(v.employee_id)
        seq = []
        for s in r.stops if r else []:
            if s.kind == "visit" and s.visit_id in task_ids and s.start > clock:
                t = next(t for t in tasks if t.visit_id == s.visit_id)
                role = s.role
                if t.roles < sc.visits[s.visit_id].required_employee_count:
                    a = plan.assignments[s.visit_id]
                    present = [i for i, e in enumerate(a.employee_ids) if e in vehicle_ids]
                    role = present.index(a.employee_ids.index(v.employee_id)) if v.employee_id in a.employee_ids else 0
                seq.append((s.visit_id, role, s.start))
        hint[v.employee_id] = seq

    fair_workloads(vehicles, sum(t.duration * t.roles for t in tasks))
    unaffected = {e for e in in_problem if e not in effect.affected_employees}
    problem = PlanningProblem(
        scenario=sc,
        travel=tm,
        weights=world.weights,
        settings=world.settings,
        vehicles=vehicles,
        tasks=tasks,
        recipient_busy=busy,
        hint_routes=hint,
        unaffected_employees=unaffected,
        label=label,
    )
    return problem, prefixes, keep_routes


def _role_fix(plan: Plan, previous: Plan, problem: PlanningProblem) -> None:
    """Restore true role indices of partial double visits (lead first)."""
    sc = problem.scenario
    for t in problem.tasks:
        v = sc.visits[t.visit_id]
        if t.roles >= v.required_employee_count or t.pinned is None:
            continue
        a_prev = previous.assignments.get(t.visit_id)
        a_new = plan.assignments.get(t.visit_id)
        if a_prev is None or a_new is None:
            continue
        order = {e: i for i, e in enumerate(a_prev.employee_ids)}
        a_new.employee_ids.sort(key=lambda e: order.get(e, 99))
        for r in plan.routes.values():
            for s in r.stops:
                if s.visit_id == t.visit_id and r.employee_id in order:
                    s.role = order[r.employee_id]


def replan(
    world: World,
    plan: Plan,
    effect: IncidentEffect,
    params: RepairParams | None = None,
    strategy: str = "baseline",
    exhaustive: bool = False,
    chooser: Any = None,
) -> ReplanResult:
    from .explain import plan_diff, replan_summary

    params = params or RepairParams()
    clock = effect.clock
    prev_planned = set(plan.assignments)
    released = {v for v in effect.released_visits if v in world.scenario.visits}
    phases: list[PhaseResult] = []
    tried_unplanned_ok = False
    best: PhaseResult | None = None

    phase_defs = [
        (1, "local repair", params.phase1_helpers, clock + params.phase1_horizon_min, params.phase1_time_s),
        (2, "expanded neighbourhood", params.phase2_helpers, None, params.phase2_time_s),
        (3, "broad re-optimisation", None, None, params.phase3_time_s),
    ]
    for ph, name, helpers, horizon, tl in phase_defs:
        if ph < params.start_phase or ph > params.max_phase:
            continue
        t0 = time.perf_counter()
        emps = None if helpers is None else _neighbourhood(world, plan, effect, clock, helpers, released)
        problem, prefixes, keep = build_repair_problem(world, plan, effect, clock, emps, horizon, f"phase {ph}: {name}")
        free = sum(1 for t in problem.tasks if not t.pinned)
        budget = min(tl, 1.5 + 0.04 * free)  # small neighbourhoods converge fast
        out = SolveEngine(problem, SolveOptions(time_limit_s=budget, enhanced_repair=params.enhanced_repair)).run()
        new_plan = assemble_plan(
            world.scenario,
            world.travel(),
            problem,
            out,
            strategy=f"{strategy} / phase {ph}",
            previous=plan,
            frozen=prefixes,
            keep_routes=keep,
            clock=clock,
        )
        _role_fix(new_plan, plan, problem)
        finalize_plan(world, new_plan, reference=plan, clock=clock)
        lost = {
            v
            for v in (prev_planned | released)
            if v in world.scenario.visits
            and world.scenario.visits[v].status != VisitStatus.CANCELLED
            and v not in new_plan.assignments
            and world.scenario.visits[v].latest_start >= clock
        }
        valid = new_plan.validation.get("valid", False)
        accepted = valid and not lost
        reason = (
            "all affected visits planned, plan valid"
            if accepted
            else ("plan failed validation" if not valid else f"{len(lost)} visit(s) left unplanned")
        )
        pr = PhaseResult(
            phase=ph,
            name=name,
            employees=len(problem.vehicles),
            free_visits=sum(1 for t in problem.tasks if not t.pinned),
            pinned_visits=sum(1 for t in problem.tasks if t.pinned),
            accepted=accepted,
            plan=new_plan,
            seconds=round(time.perf_counter() - t0, 2),
            reason=reason,
            lost=sorted(lost),
        )
        phases.append(pr)
        if valid and (best is None or _better(pr, best, world)):
            best = pr
        if accepted:
            tried_unplanned_ok = True
            if not exhaustive:
                best = pr
                break
    if chooser is not None:
        picked = chooser(phases, plan)
        if picked is not None:
            best = picked
    if best is None:
        best = phases[-1]
    chosen = best.plan
    diff = plan_diff(world.scenario, plan, chosen, clock)
    summary, facts = replan_summary(world, plan, chosen, effect, diff, best, phases)
    chosen.solver_stats["replan"] = {
        "phases": [
            {k: getattr(p, k) for k in ("phase", "name", "employees", "free_visits", "pinned_visits", "accepted", "seconds", "reason")}
            | {"unplanned": p.plan.score.get("unplanned_visits"), "valid": p.plan.validation.get("valid"), "lost": p.lost}
            for p in phases
        ],
        "strategy_used": f"phase {best.phase}: {best.name}",
        "accepted": tried_unplanned_ok,
        "params": params.to_dict(),
    }
    return ReplanResult(chosen, phases, f"phase {best.phase}: {best.name}", effect, diff, summary, facts)


def _better(a: PhaseResult, b: PhaseResult, world: World) -> bool:
    """Lexicographic: broken promises first (lost high-priority, lost visits),
    then total unplanned, then weighted cost (which includes stability)."""

    def key(p: PhaseResult) -> tuple:
        vis = world.scenario.visits
        lost_hp = sum(1 for v in p.lost if vis[v].priority >= 5)
        sc = p.plan.score
        return (lost_hp, len(p.lost), sc.get("unplanned_visits", 0), sc["weighted"]["total"])

    return key(a) < key(b)


def describe_clock(clock: int) -> str:
    return fmt_time(clock)


def clone_world(world: World) -> World:
    return World(
        scenario=copy.deepcopy(world.scenario),
        base_matrix=world.base_matrix,
        traffic=world.traffic.copy(),
        weights=copy.deepcopy(world.weights),
        settings=copy.deepcopy(world.settings),
    )


def incident_context(world: World, plan: Plan, effect: IncidentEffect) -> dict[str, Any]:
    vis = world.scenario.visits
    rel = [v for v in effect.released_visits if v in vis]
    return {
        "incident_kind": effect.kind,
        "description": effect.description,
        "clock": fmt_time(effect.clock),
        "released_visits": len(rel),
        "released_high_priority": sum(1 for v in rel if vis[v].priority >= 5),
        "released_double_staffed": sum(1 for v in rel if vis[v].required_employee_count == 2),
        "affected_employees": len(effect.affected_employees),
        "zones": sorted(effect.zones),
        "unplanned_before": plan.score.get("unplanned_visits"),
        "utilization": plan.score.get("utilization"),
        "employees_working": plan.score.get("employees_working"),
        "facts": effect.facts,
    }


def replan_with_strategy(
    world: World,
    plan: Plan,
    effect: IncidentEffect,
    strategy: str = "baseline",
    advisor: Any = None,
    classifier: Any = None,
) -> tuple[ReplanResult, dict[str, Any]]:
    """Run one re-planning strategy. Returns (result, strategy metadata)."""
    meta: dict[str, Any] = {"strategy": strategy}
    params = RepairParams()
    if strategy == "enhanced":
        params.enhanced_repair = True
    if strategy == "advisor":
        from .advisor import make_advisor

        advisor = advisor or make_advisor()
        decision = advisor.suggest_repair_strategy(incident_context(world, plan, effect))
        for k, v in decision.params.items():
            setattr(params, k, v)
        params.source = decision.source
        meta["advisor"] = decision.to_dict()
    if strategy == "classifier":
        from .classifier import Alternative, make_classifier

        classifier = classifier or make_classifier()
        params.enhanced_repair = True
        ctx = incident_context(world, plan, effect)
        meta["classification"] = classifier.classify_incident(ctx)

        def chooser(phases: list[PhaseResult], before: Plan) -> PhaseResult | None:
            alts = []
            for ph in phases:
                sc = ph.plan.score
                stab = sc.get("stability", {})
                alts.append(
                    Alternative(
                        key=str(ph.phase),
                        valid=bool(ph.plan.validation.get("valid")),
                        features={
                            "lost_high_priority": sum(1 for v in ph.lost if world.scenario.visits[v].priority >= 5),
                            "lost": len(ph.lost),
                            "unplanned": sc.get("unplanned_visits", 0),
                            "visits_changed": stab.get("visits_changed", 0),
                            "employee_changes": stab.get("employee_changes", 0),
                            "time_change_minutes": stab.get("start_time_change_minutes", 0),
                            "travel_minutes": sc.get("travel_minutes", 0),
                            "routes_changed": stab.get("routes_changed", 0),
                        },
                    )
                )
            ranking = classifier.rank_alternatives(alts)
            meta["ranking"] = ranking
            if not ranking:
                return None
            return next(p for p in phases if str(p.phase) == ranking[0][0])

        res = replan(world, plan, effect, params, strategy, exhaustive=True, chooser=chooser)
    else:
        res = replan(world, plan, effect, params, strategy)
    meta["params"] = params.to_dict()
    res.plan.solver_stats["strategy_meta"] = meta
    return res, meta
