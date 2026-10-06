"""Suggestions for unplanned visits.

For every unplanned visit three kinds of options are produced and ranked by
*severity* (0 = within all rules; lowest first):

1. ``replan``  – within the rules. A real re-optimisation of the routes of the
   5-6 nearest qualified employees together (other visits may move between
   them or in time). Accepted only if the result places the visit, loses no
   other planned visit and the independent validator says VALID.
2. ``relaxed`` – breaks rules, explicitly. The visit is inserted into one
   employee's route (two for double staffing) at every position; later stops
   are pushed. Every resulting deviation is computed and priced: start outside
   the window, visits pushed outside theirs, overtime, a meal break moved,
   more than 5 h without a rest (ATL), work in an unpaid gap, desynchronised
   double staffing, missing skill / delegation, strict wishes, single staffing
   of a double-staffed visit.
3. ``pool``    – a resource option: call in pool staff for the window.

Applying a relaxed option creates a new plan that records an *approved
exception*. The validator still reports every violation; those inside the
approved scope are listed as approved exceptions instead of silently passing.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field

from .diagnostics import _base_missing, _wish_blocks
from .domain import EmployeeStatus, Plan, RouteStop, TimingKind, Visit, fmt_time
from .planner import World, finalize_plan

# Severity points (lower = better). Calibrated so that patient-safety rules
# (delegations, skills) and employee health (allergy) rank last.
SEVERITY = {
    "soft_window_min": 1,
    "hard_window_min": 8,
    "pushed_soft_window_min": 1,
    "pushed_hard_window_min": 8,
    "overtime_min": 2,
    "break_moved_min": 2,
    "continuous_work_min": 4,
    "daily_work_min": 4,
    "gap_work_min": 3,
    "desync_double": 300,
    "gender_wish": 20,
    "language_wish": 10,
    "gender_required": 400,
    "language_required": 300,
    "smoke_free": 500,
    "pet_allergy": 800,
    "missing_skill": 600,
    "missing_delegation": 2000,
    "single_staffing": 1500,
    "pool_staff": 150,
}
NOT_PERMITTED = {"missing_delegation", "missing_skill", "pet_allergy", "single_staffing"}


@dataclass
class Violation:
    code: str
    text: str
    points: int
    minutes: int = 0

    def to_dict(self) -> dict:
        return {"code": self.code, "text": self.text, "points": self.points, "minutes": self.minutes}


@dataclass
class Insertion:
    """Relaxed insertion of one role into one route."""

    employee_id: str
    role: int
    position: int
    start: int
    new_starts: dict[int, int]  # stop index (old route) -> new start
    route_end: int
    route_start: int | None
    violations: list[Violation] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)

    @property
    def points(self) -> int:
        return sum(v.points for v in self.violations)


def _level(points: int, codes: set[str]) -> str:
    if points == 0:
        return "within_rules"
    if codes & NOT_PERMITTED:
        return "not_permitted"
    if points < 100:
        return "minor"
    return "major"


# ---------------------------------------------------------------------------
# Relaxed insertion
# ---------------------------------------------------------------------------


def _qual_violations(world: World, eid: str, v: Visit, role: int) -> list[Violation]:
    sc = world.scenario
    out = []
    for m in _base_missing(sc, eid, v, role):
        if m.startswith("skill"):
            out.append(Violation("missing_skill", f"{eid} lacks {m}", SEVERITY["missing_skill"]))
        elif m.startswith("delegation"):
            out.append(Violation("missing_delegation", f"{eid} lacks {m} (not permitted: patient safety)",
                                 SEVERITY["missing_delegation"]))
        else:
            out.append(Violation("missing_skill", f"{eid}: {m}", SEVERITY["missing_skill"]))
    for m in _wish_blocks(sc, eid, v, role):
        code = ("gender_required" if "staff required" in m else "pet_allergy" if "allergy" in m
                else "smoke_free" if "smoke" in m else "language_required")
        out.append(Violation(code, f"{eid}: {m}", SEVERITY[code]))
    r = sc.recipients[v.recipient_id]
    e = sc.employees[eid]
    if r.gender_preference and not r.gender_strict and (r.gender_scope == "all" or v.intimate_care) \
            and e.gender != r.gender_preference:
        out.append(Violation("gender_wish", f"recipient wishes {'female' if r.gender_preference == 'F' else 'male'} staff",
                             SEVERITY["gender_wish"]))
    if r.languages and not r.language_required and not set(r.languages) & set(e.languages):
        out.append(Violation("language_wish", f"{eid} does not speak {'/'.join(r.languages)}", SEVERITY["language_wish"]))
    return out


def _evaluate(world: World, plan: Plan, eid: str, v: Visit, role: int, pos: int,
              forced_start: int | None = None) -> Insertion | None:
    sc = world.scenario
    e = sc.employees[eid]
    rules = sc.rules
    etm = world.travel().for_employee(e)
    route = plan.routes.get(eid)
    stops = sorted(route.stops, key=lambda s: s.start) if route else []
    clock = plan.clock
    viol: list[Violation] = []
    start_loc = (route.start_location_id if route and route.start_location_id else e.start_location_id)
    end_loc = (route.end_location_id if route and route.end_location_id else e.end_location_id)
    avail = max(e.shift_start, e.available_from or 0, clock or 0)
    if pos == 0:
        prev_loc, prev_end = start_loc, avail  # same reference point as the validator
    else:
        p = stops[pos - 1]
        if clock is not None and p.end > clock and pos < len(stops) and stops[pos].start <= clock:
            return None
        prev_loc, prev_end = p.location_id, max(p.end, avail)
    if pos < len(stops) and clock is not None and stops[pos].start <= clock:
        return None  # cannot insert before something already started
    arr = prev_end + etm.minutes(prev_loc, v.location_id)
    start = max(arr, v.earliest_start) if forced_start is None else forced_start
    if forced_start is not None and forced_start < arr:
        return None
    hard = v.timing == TimingKind.HARD
    if start > v.latest_start:
        m = start - v.latest_start
        code = "hard_window_min" if hard else "soft_window_min"
        viol.append(Violation(code, f"starts {m} min after its {'time-critical ' if hard else ''}window "
                              f"({fmt_time(v.earliest_start)}-{fmt_time(v.latest_start)})", SEVERITY[code] * m, m))
    for iv in e.unavailable:
        if start < iv.end and iv.start < start + v.total_duration_minutes:
            if iv.reason.startswith("sick") or iv.end >= e.shift_end:
                return None
            m = min(iv.end, start + v.total_duration_minutes) - max(iv.start, start)
            viol.append(Violation("gap_work_min", f"works {m} min into the unpaid gap {fmt_time(iv.start)}-{fmt_time(iv.end)}",
                                  SEVERITY["gap_work_min"] * m, m))
    cur_end, cur_loc = start + v.total_duration_minutes, v.location_id
    new_starts: dict[int, int] = {}
    pushed: list[str] = []
    for k in range(pos, len(stops)):
        s = stops[k]
        tr = 0 if s.kind == "unavailable" else etm.minutes(cur_loc, s.location_id)
        ns = max(s.start, cur_end + tr)
        if ns > s.start:
            if s.locked or (clock is not None and s.start <= clock):
                return None
            delay = ns - s.start
            if s.kind == "visit" and s.visit_id in sc.visits:
                vv = sc.visits[s.visit_id]
                pushed.append(s.visit_id)
                if ns > vv.latest_start:
                    m = ns - vv.latest_start
                    h = vv.timing == TimingKind.HARD
                    code = "pushed_hard_window_min" if h else "pushed_soft_window_min"
                    viol.append(Violation(code, f"{vv.id} pushed {m} min past its window", SEVERITY[code] * m, m))
                if vv.required_employee_count == 2:
                    viol.append(Violation("desync_double", f"double-staffed {vv.id} pushed {delay} min: partner must move too",
                                          SEVERITY["desync_double"], delay))
            elif s.kind == "break":
                b = e.breaks[s.role] if s.role < len(e.breaks) else None
                if b is not None and ns > b.latest_start:
                    m = ns - b.latest_start
                    viol.append(Violation("break_moved_min", f"meal break starts {m} min later than its window",
                                          SEVERITY["break_moved_min"] * m, m))
            elif s.kind == "unavailable":
                viol.append(Violation("gap_work_min", f"works {delay} min into the unpaid gap", SEVERITY["gap_work_min"] * delay, delay))
                cur_end, cur_loc = max(s.end, ns), s.location_id
                continue
            new_starts[k] = ns
        cur_end, cur_loc = ns + (s.end - s.start), s.location_id
    route_end = cur_end + etm.minutes(cur_loc, end_loc)
    hard_end = e.shift_end + world.settings.max_overtime_min
    if route_end > hard_end:
        m = route_end - hard_end
        viol.append(Violation("overtime_min", f"{m} min overtime (back {fmt_time(route_end)}, shift ends {fmt_time(e.shift_end)})",
                              SEVERITY["overtime_min"] * m, m))
    route_start = route.route_start if route else None
    if pos == 0:
        depart = start - etm.minutes(prev_loc, v.location_id)
        route_start = depart if route_start is None else min(route_start, depart)
    # Arbetstidslagen on the new day
    if rules.enabled and route_start is not None:
        timeline = [(start, start + v.total_duration_minutes, "visit")]
        for k, s in enumerate(stops):
            st = new_starts.get(k, s.start)
            timeline.append((st, st + (s.end - s.start), s.kind))
        timeline.sort()
        seg, worked, longest = route_start, 0, 0
        for a, b, kind in timeline:
            if kind in ("break", "unavailable") and b - a >= 30:
                longest = max(longest, a - seg)
                worked += max(0, a - seg)
                seg = b
        longest = max(longest, route_end - seg)
        worked += max(0, route_end - seg)
        if longest > rules.max_continuous_work_min:
            m = longest - rules.max_continuous_work_min
            viol.append(Violation("continuous_work_min", f"{longest // 60} h {longest % 60} min without a rest (ATL max 5 h)",
                                  SEVERITY["continuous_work_min"] * m, m))
        if worked > rules.max_daily_work_min:
            m = worked - rules.max_daily_work_min
            viol.append(Violation("daily_work_min", f"{m} min over the 10 h daily maximum", SEVERITY["daily_work_min"] * m, m))
    viol += _qual_violations(world, eid, v, role)
    return Insertion(eid, role, pos, start, new_starts, route_end, route_start, viol, pushed)


def _best_insertions(world: World, plan: Plan, v: Visit, role: int, limit: int = 8,
                     forced_start: int | None = None, only: str | None = None) -> list[Insertion]:
    sc = world.scenario
    out: list[Insertion] = []
    for eid, e in sc.employees.items():
        if only is not None and eid != only:
            continue
        if e.status != EmployeeStatus.WORKING:
            continue
        if e.shift_start > v.latest_start + 60 or e.shift_end < v.earliest_start - 30:
            continue
        r = plan.routes.get(eid)
        n = len(r.stops) if r else 0
        best = None
        for pos in range(n + 1):
            ins = _evaluate(world, plan, eid, v, role, pos, forced_start)
            if ins is not None and (best is None or ins.points < best.points):
                best = ins
        if best is not None:
            out.append(best)
    out.sort(key=lambda i: (i.points, i.start))
    return out[:limit]


def relaxed_options(world: World, plan: Plan, v: Visit, limit: int = 4) -> list[dict]:
    opts: list[dict] = []
    if v.required_employee_count == 1:
        for ins in _best_insertions(world, plan, v, 0, limit):
            opts.append(_option_from([ins], v))
        return opts
    leads = _best_insertions(world, plan, v, 0, 6)
    assists = _best_insertions(world, plan, v, 1, 6)
    combos = []
    for a in leads:
        for b in assists:
            if a.employee_id == b.employee_id:
                continue
            t = max(a.start, b.start)
            a2 = a if a.start == t else _forced(world, plan, v, a, t)
            b2 = b if b.start == t else _forced(world, plan, v, b, t)
            if a2 is None or b2 is None:
                continue
            combos.append((a2.points + b2.points, a2, b2))
    combos.sort(key=lambda c: c[0])
    seen = set()
    for _, a, b in combos:
        key = (a.employee_id, b.employee_id)
        if key in seen:
            continue
        seen.add(key)
        opts.append(_option_from([a, b], v))
        if len(opts) >= limit - 1:
            break
    if leads:
        single = copy.deepcopy(leads[0])
        single.violations.append(Violation("single_staffing", "double-staffed visit done by one employee (not permitted: "
                                           "lifting / transfer safety)", SEVERITY["single_staffing"]))
        opts.append(_option_from([single], v))
    opts.sort(key=lambda o: o["severity"])
    return opts[:limit]


def _forced(world: World, plan: Plan, v: Visit, ins: Insertion, t: int) -> Insertion | None:
    return _evaluate(world, plan, ins.employee_id, v, ins.role, ins.position, forced_start=t)


def _option_from(parts: list[Insertion], v: Visit) -> dict:
    viol = [x for p in parts for x in p.violations]
    pts = sum(x.points for x in viol)
    codes = {x.code for x in viol}
    who = " + ".join(p.employee_id for p in parts)
    start = min(p.start for p in parts)
    return {
        "id": "S-" + uuid.uuid4().hex[:8],
        "kind": "relaxed",
        "visit_id": v.id,
        "severity": pts,
        "level": _level(pts, codes),
        "title": f"{who} at {fmt_time(start)}" + (" (within rules)" if pts == 0 else ""),
        "employees": [p.employee_id for p in parts],
        "start": start,
        "violations": [x.to_dict() for x in viol],
        "pushed_visits": sorted({x for p in parts for x in p.pushed}),
        "_parts": parts,
    }


# ---------------------------------------------------------------------------
# Re-optimisation within the rules
# ---------------------------------------------------------------------------


def _candidates(world: World, plan: Plan, v: Visit, k: int) -> list[str]:
    sc = world.scenario
    tm = world.travel()
    roles = range(v.required_employee_count)
    scored = []
    for eid, e in sc.employees.items():
        if e.status != EmployeeStatus.WORKING:
            continue
        if e.shift_start > v.latest_start or e.shift_end < v.earliest_start + v.total_duration_minutes:
            continue
        if all(_base_missing(sc, eid, v, r) or _wish_blocks(sc, eid, v, r) for r in roles):
            continue
        r = plan.routes.get(eid)
        near = [s for s in (r.stops if r else []) if abs(s.start - v.preferred_start) <= 120]
        locs = [s.location_id for s in near] or [e.start_location_id]
        dist = min(tm.minutes(l, v.location_id) for l in locs)
        load = sum(s.end - s.start for s in near)
        scored.append((dist + load / 20, eid))
    scored.sort()
    return [e for _, e in scored[:k]]


def replan_option(world: World, plan: Plan, v: Visit, time_s: float = 3.0) -> dict | None:
    from .incidents import IncidentEffect
    from .replanning import RepairParams, replan

    cands = _candidates(world, plan, v, 6 if v.required_employee_count == 2 else 5)
    if len(cands) < v.required_employee_count:
        return None
    clock = plan.clock if plan.clock is not None else 0
    effect = IncidentEffect(
        kind="suggestion", clock=clock, description=f"Try to plan {v.id} by re-planning {', '.join(cands)}.",
        affected_employees=set(cands), released_visits={v.id},
    )
    params = RepairParams()
    params.start_phase, params.max_phase = 1, 1
    params.phase1_helpers, params.phase1_horizon_min, params.phase1_time_s = 0, 24 * 60, time_s
    res = replan(world, plan, effect, params, "suggestion")
    np = res.plan
    lost = [x for x in plan.assignments if x not in np.assignments and world.scenario.visits[x].latest_start >= clock]
    if v.id not in np.assignments or lost or not np.validation.get("valid"):
        return None
    changed = res.diff["counts"]["visits_changed"]
    a = np.assignments[v.id]
    extra = sorted(x for x in np.assignments if x not in plan.assignments and x != v.id)
    return {
        "id": "S-" + uuid.uuid4().hex[:8],
        "kind": "replan",
        "visit_id": v.id,
        "severity": 0,
        "level": "within_rules",
        "title": f"Re-plan {len(cands)} routes: {' + '.join(a.employee_ids)} at {fmt_time(a.start)}",
        "employees": a.employee_ids,
        "start": a.start,
        "violations": [],
        "pushed_visits": [],
        "changes": changed,
        "also_planned": extra,
        "routes": cands,
        "_plan": np,
    }


def pool_option(world: World, v: Visit) -> dict:
    r = world.scenario.recipients[v.recipient_id]
    quals = sorted(set(v.required_delegations) | set(v.required_skills))
    return {
        "id": "S-" + uuid.uuid4().hex[:8],
        "kind": "pool",
        "visit_id": v.id,
        "severity": SEVERITY["pool_staff"],
        "level": "resource",
        "title": f"Call in pool staff for {r.zone} from {fmt_time(max(6 * 60 + 30, v.earliest_start - 30))}"
                 + (f" with {', '.join(quals)}" if quals else ""),
        "employees": [],
        "start": v.earliest_start,
        "violations": [{"code": "pool_staff", "text": "extra staff hours (cost, not a rule break)",
                        "points": SEVERITY["pool_staff"], "minutes": 0}],
        "pushed_visits": [],
        "team": r.zone,
        "clock": max(6 * 60, v.earliest_start - 60),
        "delegations": sorted(v.required_delegations),
        "skills": sorted(v.required_skills),
    }


def suggest_for_plan(world: World, plan: Plan, visit_ids: list[str] | None = None, replan_time_s: float = 3.0,
                     progress=None) -> dict[str, list[dict]]:
    sc = world.scenario
    ids = visit_ids if visit_ids is not None else sorted(plan.unplanned, key=lambda x: (-sc.visits[x].priority, x))
    out: dict[str, list[dict]] = {}
    for i, vid in enumerate(ids):
        v = sc.visits.get(vid)
        if v is None or (plan.clock is not None and v.latest_start < plan.clock):
            continue
        if progress:
            progress(f"{i + 1}/{len(ids)}: {vid}")
        opts: list[dict] = []
        rp = replan_option(world, plan, v, replan_time_s)
        if rp:
            opts.append(rp)
        opts += relaxed_options(world, plan, v)
        opts.append(pool_option(world, v))
        opts.sort(key=lambda o: (o["severity"], o.get("changes", 0)))
        out[vid] = opts
    return out


def public(opt: dict) -> dict:
    return {k: v for k, v in opt.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Applying a relaxed option (approved exception)
# ---------------------------------------------------------------------------


def apply_relaxed(world: World, plan: Plan, opt: dict, approved_by: str = "planner") -> Plan:
    from .plan_builder import _refresh_travel, new_plan_id

    sc = world.scenario
    v = sc.visits[opt["visit_id"]]
    new = copy.deepcopy(plan)
    new.id = new_plan_id()
    new.parent_plan_id = plan.id
    new.strategy = "manual exception"
    scope_visits = {v.id} | set(opt["pushed_visits"])
    starts: dict[str, int] = {}
    for ins in opt["_parts"]:
        e = sc.employees[ins.employee_id]
        r = new.routes[ins.employee_id]
        stops = sorted(r.stops, key=lambda s: s.start)
        for k, ns in ins.new_starts.items():
            s = stops[k]
            d = ns - s.start
            s.start, s.end = ns, s.end + d
        stops.insert(ins.position, RouteStop("visit", v.id, ins.role, v.location_id, ins.start,
                                             ins.start + v.total_duration_minutes, 0, ""))
        r.stops = stops
        if r.route_start is None or (ins.route_start is not None and ins.route_start < r.route_start):
            r.route_start = ins.route_start
        r.route_end = ins.route_end
        if not r.start_location_id:
            r.start_location_id, r.end_location_id = e.start_location_id, e.end_location_id
        _refresh_travel(r, world.travel().for_employee(e), e.start_location_id, new.clock)
        starts[ins.employee_id] = ins.start
    from .domain import Assignment

    new.assignments[v.id] = Assignment(v.id, [p.employee_id for p in opt["_parts"]], min(starts.values()),
                                       max(starts.values()) + v.total_duration_minutes, starts)
    for pv in opt["pushed_visits"]:
        a = new.assignments.get(pv)
        if a is None:
            continue
        for eid in a.employee_ids:
            st = next(s for s in new.routes[eid].stops if s.visit_id == pv)
            a.starts[eid] = st.start
        a.start = min(a.starts.values())
        a.end = a.start + sc.visits[pv].total_duration_minutes
    new.unplanned.pop(v.id, None)
    new.exceptions = list(plan.exceptions) + [{
        "visit_id": v.id,
        "employees": [p.employee_id for p in opt["_parts"]],
        "visits": sorted(scope_visits),
        "violations": [x for x in opt["violations"]],
        "severity": opt["severity"],
        "approved_by": approved_by,
    }]
    return finalize_plan(world, new, reference=plan, clock=new.clock)


def apply_option(world: World, plan: Plan, opt: dict) -> Plan:
    if opt["kind"] == "replan":
        np = opt["_plan"]
        np.parent_plan_id = plan.id
        np.strategy = "suggestion: re-plan within rules"
        return np
    if opt["kind"] == "relaxed":
        return apply_relaxed(world, plan, opt)
    raise ValueError("pool options are applied as an 'extra staff' incident")


def deep_replan(world: World, plan: Plan, time_s: float = 45.0) -> Plan | None:
    """Re-optimise everything that has not started, warm-started from ``plan``.
    Returned only if it plans more visits, loses none and validates."""
    from .incidents import IncidentEffect
    from .replanning import RepairParams, replan

    if plan.clock is None:
        # Before the day starts nothing is communicated yet: optimise freely,
        # warm-started from the current plan.
        from .plan_builder import assemble_plan
        from .solver.builder import build_global_problem
        from .solver.engine import SolveEngine, SolveOptions

        tm = world.travel()
        problem = build_global_problem(world.scenario, tm, world.weights, world.settings)
        problem.hint_routes = {
            eid: [(s.visit_id, s.role, s.start) for s in sorted(r.stops, key=lambda s: s.start) if s.kind == "visit"]
            for eid, r in plan.routes.items()
        }
        out = SolveEngine(problem, SolveOptions(time_limit_s=time_s, seed=7)).run()
        np = finalize_plan(world, assemble_plan(world.scenario, tm, problem, out, strategy="deep re-plan"))
        if not np.validation.get("valid") or len(np.unplanned) >= len(plan.unplanned):
            return None
        np.parent_plan_id = plan.id
        return np

    clock = plan.clock
    effect = IncidentEffect(kind="deep re-plan", clock=clock, description="Deep re-optimisation of all open visits.")
    params = RepairParams()
    params.start_phase, params.max_phase, params.phase3_time_s = 3, 3, time_s
    res = replan(world, plan, effect, params, "deep re-plan")
    np = res.plan
    lost = [x for x in plan.assignments if x not in np.assignments and world.scenario.visits[x].latest_start >= clock]
    if lost or not np.validation.get("valid") or len(np.unplanned) >= len(plan.unplanned):
        return None
    np.parent_plan_id = plan.id
    return np


def autofix(world: World, plan: Plan, time_s: float = 3.0, progress=None, deep_s: float = 45.0,
            budget_s: float = 120.0) -> tuple[Plan, dict]:
    """Go through every unplanned visit and fix what can be fixed within the rules:
    first a deep re-optimisation of all open visits, then a local re-plan of the
    nearest qualified routes per remaining visit (highest priority first)."""
    import time as _t

    t0 = _t.perf_counter()
    sc = world.scenario
    ids = sorted(plan.unplanned, key=lambda x: (-sc.visits[x].priority, x))
    cur = plan
    deep_fixed = 0
    if deep_s > 0 and ids:
        if progress:
            progress(f"deep re-optimisation of all open visits ({int(deep_s)} s)")
        d = deep_replan(world, plan, deep_s)
        if d is not None:
            deep_fixed = len(plan.unplanned) - len(d.unplanned)
            cur = d
    fixed, failed = [], []
    rest = [v for v in ids if v in cur.unplanned]
    for i, vid in enumerate(rest):
        if _t.perf_counter() - t0 > budget_s:
            break
        if vid not in cur.unplanned:
            continue
        v = sc.visits[vid]
        if cur.clock is not None and v.latest_start < cur.clock:
            continue
        if progress:
            progress(f"local re-plan {i + 1}/{len(rest)}: {vid} ({deep_fixed + len(fixed)} fixed so far)")
        opt = replan_option(world, cur, v, time_s)
        if opt is None:
            failed.append(vid)
            continue
        cur = apply_option(world, cur, opt)
        fixed.append(vid)
    if cur is not plan:
        cur.strategy = "auto-fix within rules"
        cur.parent_plan_id = plan.id
    planned_now = [v for v in ids if v not in cur.unplanned]
    newly_unplanned = sorted(v for v in cur.unplanned if v not in plan.unplanned)
    net = len(ids) - len(cur.unplanned)
    summary = {
        "unplanned_before": len(ids),
        "unplanned_after": len(cur.unplanned),
        "net_planned_gain": net,
        "fixed_within_rules": net,
        "fixed_share": round(net / max(1, len(ids)), 3),
        "fixed_by_deep_replan": deep_fixed,
        "fixed_by_local_replan": len(fixed),
        "originally_unplanned_now_planned": len(planned_now),
        "swapped_out": newly_unplanned,  # free re-plan before the day: other visits now unplanned instead
        "interventions_planned_share_before": plan.score.get("interventions_planned_share"),
        "interventions_planned_share_after": cur.score.get("interventions_planned_share"),
        "still_unplanned": len(cur.unplanned),
        "not_fixed": [v for v in ids if v in cur.unplanned],
        "seconds": round(_t.perf_counter() - t0, 1),
    }
    return cur, summary
