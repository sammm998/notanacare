"""Operational incidents and how they change the world.

Each ``apply_*`` mutates the :class:`World` (scenario + traffic) and returns an
:class:`IncidentEffect`: structured facts used by local repair and by the
human-readable summary. Nothing here plans anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import catalog as C
from .domain import (
    BreakRule,
    Employee,
    EmployeeStatus,
    Intervention,
    Interval,
    Plan,
    TimingKind,
    Visit,
    VisitStatus,
    fmt_time,
)
from .planner import World
from .travel import TrafficConditions
from .visit_builder import _make_visit

INCIDENT_KINDS = (
    "employee_sick",
    "medication_moved",
    "traffic",
    "employee_delayed",
    "extra_staff",
    "visit_cancelled",
    "child_sick",
    "alarm",
)


@dataclass(slots=True)
class Incident:
    kind: str
    clock: int  # simulation time at which it is reported
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "clock": self.clock, "params": self.params}

    @classmethod
    def from_dict(cls, d: dict) -> "Incident":
        return cls(kind=d["kind"], clock=int(d["clock"]), params=dict(d.get("params") or {}))


@dataclass(slots=True)
class IncidentEffect:
    kind: str
    clock: int
    description: str
    affected_employees: set[str] = field(default_factory=set)
    released_visits: set[str] = field(default_factory=set)  # must be re-planned
    changed_visits: set[str] = field(default_factory=set)  # requirements changed
    zones: set[str] = field(default_factory=set)
    facts: dict[str, Any] = field(default_factory=dict)
    widen_to_unplanned: bool = False  # e.g. extra staff: try previously unplanned visits

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "clock": self.clock,
            "description": self.description,
            "affected_employees": sorted(self.affected_employees),
            "released_visits": sorted(self.released_visits),
            "changed_visits": sorted(self.changed_visits),
            "zones": sorted(self.zones),
            "facts": self.facts,
        }


def _future_visits_of(plan: Plan, eid: str, clock: int) -> list[str]:
    r = plan.routes.get(eid)
    if not r:
        return []
    return [s.visit_id for s in r.stops if s.kind == "visit" and s.visit_id and s.start > clock]


def _partners(plan: Plan, visit_ids: set[str]) -> set[str]:
    out = set()
    for vid in visit_ids:
        a = plan.assignments.get(vid)
        if a:
            out |= set(a.employee_ids)
    return out


def apply_incident(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    handler = {
        "employee_sick": _sick,
        "medication_moved": _medication_moved,
        "traffic": _traffic,
        "employee_delayed": _delayed,
        "extra_staff": _extra_staff,
        "visit_cancelled": _cancelled,
        "child_sick": _child_sick,
        "alarm": _alarm,
    }.get(inc.kind)
    if handler is None:
        raise ValueError(f"Unknown incident kind '{inc.kind}'. Known: {INCIDENT_KINDS}")
    return handler(world, plan, inc)


# ---------------------------------------------------------------------------


def _sick(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    ids = inc.params.get("employee_ids") or [inc.params.get("employee_id")]
    ids = [e for e in ids if e]
    if not ids:
        raise ValueError("employee_sick needs employee_id(s)")
    clock = inc.clock
    released: set[str] = set()
    names = []
    for eid in ids:
        emp = world.scenario.employees[eid]
        names.append(eid)
        if clock <= emp.shift_start:
            emp.status = EmployeeStatus.SICK
        else:
            # Work already started (an active visit) is finished; nothing after.
            r = plan.routes.get(eid)
            busy_until = clock
            if r:
                for s in r.stops:
                    if s.start <= clock < s.end:
                        busy_until = max(busy_until, s.end)
            emp.unavailable.append(Interval(busy_until, max(busy_until, emp.shift_end), "sick"))
        released |= set(_future_visits_of(plan, eid, clock))
    unlocked = sorted(v for v in released if world.scenario.visits[v].locked)
    for v in unlocked:
        world.scenario.visits[v].locked = False  # cannot be kept: its employee is gone
    affected = set(ids) | _partners(plan, released)
    return IncidentEffect(
        kind=inc.kind,
        clock=clock,
        description=f"Employee{'s' if len(ids) > 1 else ''} {', '.join(names)} reported sick at {fmt_time(clock)}.",
        affected_employees=affected,
        released_visits=released,
        facts={"sick_employees": ids, "future_visits_affected": len(released), "unlocked_visits": unlocked},
    )


def _delayed(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    eid = inc.params["employee_id"]
    minutes = int(inc.params.get("minutes", 25))
    emp: Employee = world.scenario.employees[eid]
    clock = inc.clock
    r = plan.routes.get(eid)
    ready = clock
    loc = emp.start_location_id
    if r:
        for s in r.stops:
            if s.start <= clock:
                ready = max(ready, s.end)
                loc = s.location_id
    emp.available_from = ready + minutes
    emp.current_location_id = loc
    released = set(_future_visits_of(plan, eid, clock))
    return IncidentEffect(
        kind=inc.kind,
        clock=clock,
        description=f"{eid} is delayed {minutes} min at {fmt_time(clock)} (available from {fmt_time(emp.available_from)}).",
        affected_employees={eid} | _partners(plan, released),
        released_visits=released,
        facts={"employee": eid, "delay_minutes": minutes, "available_from": emp.available_from},
    )


def _traffic(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    p = inc.params
    tc: TrafficConditions = world.traffic.copy()
    zones: set[str] = set()
    if p.get("global_multiplier"):
        tc.global_multiplier = float(p["global_multiplier"])
    zone = p.get("zone")
    if zone:
        tc.zone_multipliers[zone] = float(p.get("multiplier", 1.6))
        zones.add(zone)
    corridor = p.get("corridor")
    if corridor:
        a, b = corridor
        tc.corridor_multipliers[TrafficConditions.corridor_key(a, b)] = float(p.get("multiplier", 1.6))
        zones |= {a, b}
    old_tm = world.travel()
    world.traffic = tc
    new_tm = world.travel()
    # Employees whose remaining day is no longer travel-feasible, or who work in the zone.
    affected: set[str] = set()
    late_visits: set[str] = set()
    for eid, r in plan.routes.items():
        stops = [s for s in r.stops if s.start > inc.clock]
        if not stops:
            continue
        emp = world.scenario.employees[eid]
        etm = new_tm.for_employee(emp)
        prev = None
        for s in r.stops:
            if prev is not None and s.start > inc.clock:
                if prev.end + etm.minutes(prev.location_id, s.location_id) > s.start:
                    affected.add(eid)
                    if s.visit_id:
                        late_visits.add(s.visit_id)
            prev = s
        if zones and any(world.scenario.locations[s.location_id].zone in zones for s in stops):
            affected.add(eid)
        last = r.stops[-1]
        ret = last.end + etm.minutes(last.location_id, r.end_location_id or emp.end_location_id)
        if ret > emp.shift_end + world.settings.max_overtime_min:
            affected.add(eid)  # can no longer get back before the shift ends
    released = set()
    for eid in affected:
        released |= set(_future_visits_of(plan, eid, inc.clock))
    what = []
    if p.get("global_multiplier"):
        what.append(f"all travel x{float(p['global_multiplier']):.2f}")
    if zone:
        what.append(f"{zone} x{float(p.get('multiplier', 1.6)):.2f}")
    if corridor:
        what.append(f"{corridor[0]}<->{corridor[1]} x{float(p.get('multiplier', 1.6)):.2f}")
    return IncidentEffect(
        kind=inc.kind,
        clock=inc.clock,
        description=f"Traffic disruption at {fmt_time(inc.clock)}: {', '.join(what) or 'no change'}.",
        affected_employees=affected | _partners(plan, released),
        released_visits=released,
        zones=zones,
        facts={
            "routes_now_infeasible": len({e for e in affected if any(v in late_visits for v in _future_visits_of(plan, e, inc.clock))}),
            "visits_now_late": len(late_visits),
            "late_visit_ids": sorted(late_visits),
            "old_source": old_tm.source,
        },
    )


def _medication_moved(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    """Move a time-critical intervention (e.g. insulin 11:00 -> 09:00)."""
    sc = world.scenario
    p = inc.params
    new_time = int(p["new_time"])
    half = int(p.get("window_minutes", 15))
    iid = p.get("intervention_id")
    vid = p.get("visit_id")
    if not iid:
        v = sc.visits[vid]
        crit = [i for i in v.intervention_ids if sc.interventions[i].type in ("insulin", "medication", "eye_drops")]
        if not crit:
            raise ValueError(f"{vid} contains no medication / injection intervention")
        iid = next((i for i in crit if sc.interventions[i].type == "insulin"), crit[0])
    it = sc.interventions[iid]
    old_visit = next(v for v in sc.visits.values() if iid in v.intervention_ids)
    if old_visit.id in plan.assignments and plan.assignments[old_visit.id].start <= inc.clock:
        raise ValueError(f"{old_visit.id} has already started; it cannot be changed")
    old_pref = it.preferred_time
    it.preferred_time = new_time
    it.earliest_start = max(0, new_time - half)
    it.latest_start = new_time + half
    it.timing = TimingKind.HARD
    recipient = sc.recipients[it.recipient_id]
    changed = {old_visit.id}
    action = ""

    def rebuild(v: Visit, ids: list[str]) -> Visit:
        nv = _make_visit(v.id, recipient, [sc.interventions[i] for i in ids])
        nv.slot = v.slot
        nv.locked = v.locked
        return nv

    candidate = rebuild(old_visit, old_visit.intervention_ids)
    if candidate.earliest_start <= candidate.latest_start:
        sc.visits[old_visit.id] = candidate
        action = f"kept in {old_visit.id}; its start window is now {fmt_time(candidate.earliest_start)}-{fmt_time(candidate.latest_start)}"
    else:
        rest = [i for i in old_visit.intervention_ids if i != iid]
        target = None
        for v in sorted(sc.visits.values(), key=lambda v: v.id):
            if v.recipient_id != recipient.id or v.id == old_visit.id or v.status == VisitStatus.CANCELLED:
                continue
            if v.id in plan.assignments and plan.assignments[v.id].start <= inc.clock:
                continue
            merged = rebuild(v, v.intervention_ids + [iid])
            if merged.earliest_start <= merged.latest_start:
                target = merged
                break
        if rest:
            sc.visits[old_visit.id] = rebuild(old_visit, rest)
        else:
            sc.visits[old_visit.id].status = VisitStatus.CANCELLED
        if target is not None:
            sc.visits[target.id] = target
            changed.add(target.id)
            action = f"moved from {old_visit.id} into {target.id} (window {fmt_time(target.earliest_start)}-{fmt_time(target.latest_start)})"
        else:
            nid = f"{old_visit.id}-S{sum(1 for k in sc.visits if k.startswith(old_visit.id + '-S')) + 1}"
            nv = _make_visit(nid, recipient, [it])
            nv.slot = "extra"
            sc.visits[nid] = nv
            changed.add(nid)
            action = f"split out of {old_visit.id} into new visit {nid} ({fmt_time(nv.earliest_start)}-{fmt_time(nv.latest_start)})"
    affected = _partners(plan, changed)
    return IncidentEffect(
        kind=inc.kind,
        clock=inc.clock,
        description=(
            f"{C.INTERVENTION_BY_KEY[it.type].label} for {recipient.id} moved from {fmt_time(old_pref)} to "
            f"{fmt_time(new_time)} (±{half} min) at {fmt_time(inc.clock)}; {action}."
        ),
        affected_employees=affected,
        released_visits=set(changed),
        changed_visits=changed,
        zones={recipient.zone},
        facts={"intervention": iid, "old_time": old_pref, "new_time": new_time, "action": action},
    )


def _extra_staff(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    sc = world.scenario
    n = int(inc.params.get("count", 2))
    start = max(inc.clock + int(inc.params.get("lead_minutes", 30)), 6 * 60 + 30)
    end = int(inc.params.get("shift_end", 22 * 60 + 15))
    team = inc.params.get("team") or sc.locations[sc.bases[0]].zone
    base = next(b for b in sc.bases if sc.locations[b].zone == team)
    added = []
    for k in range(n):
        eid = f"E-X{len([e for e in sc.employees if e.startswith('E-X')]) + 1:02d}"
        breaks = [BreakRule(30, min(start + 180, end - 60), min(start + 300, end - 30))] if end - start > 330 else []
        sc.employees[eid] = Employee(
            id=eid,
            name=f"Pool staff {eid[-2:]} (extra)",
            team=team,
            shift_start=start,
            shift_end=end,
            start_location_id=base,
            end_location_id=base,
            skills=sorted({C.HOIST, C.DEMENTIA} | set(inc.params.get("skills") or [])),
            delegations=sorted({C.MEDICATION, C.INSULIN} | set(inc.params.get("delegations") or [])),
            breaks=breaks,
        )
        added.append(eid)
    return IncidentEffect(
        kind=inc.kind,
        clock=inc.clock,
        description=f"{n} extra pool employee(s) ({', '.join(added)}) available {fmt_time(start)}-{fmt_time(end)} from team {team}.",
        affected_employees=set(added),
        widen_to_unplanned=True,
        zones={team},
        facts={"added": added},
    )


def _cancelled(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    vid = inc.params["visit_id"]
    v = world.scenario.visits[vid]
    v.status = VisitStatus.CANCELLED
    affected = _partners(plan, {vid})
    return IncidentEffect(
        kind=inc.kind,
        clock=inc.clock,
        description=f"{vid} for {v.recipient_id} cancelled at {fmt_time(inc.clock)} ({inc.params.get('reason', 'recipient unavailable')}).",
        affected_employees=affected,
        changed_visits={vid},
        widen_to_unplanned=True,
        facts={"cancelled": vid},
    )


def _position_at(world: World, plan: Plan, eid: str, clock: int) -> tuple[str, int, str]:
    """(location, time free, what they are doing) for an employee at ``clock``.
    An ongoing visit or break is finished first; between stops the employee is
    counted from the last stop (travel already under way is not credited)."""
    emp = world.scenario.employees[eid]
    loc, free, doing = emp.current_location_id or emp.start_location_id, max(clock, emp.shift_start), "at the office"
    if emp.available_from is not None:
        free = max(free, emp.available_from)
    r = plan.routes.get(eid)
    for s in sorted(r.stops if r else [], key=lambda s: s.start):
        if s.start <= clock:
            loc = s.location_id
            if s.end > clock:
                free = max(free, s.end)
                doing = f"finishing {s.visit_id or s.kind} until {fmt_time(s.end)}"
            else:
                doing = f"after {s.visit_id or s.kind}"
    return loc, free, doing


def _child_sick(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    """VAB: the employee finishes the visit in progress, then leaves to pick up a sick child."""
    eid = inc.params["employee_id"]
    back = inc.params.get("return_minutes")
    emp = world.scenario.employees[eid]
    clock = inc.clock
    _, busy_until, doing = _position_at(world, plan, eid, clock)
    until = emp.shift_end if not back else min(emp.shift_end, busy_until + int(back))
    emp.unavailable.append(Interval(busy_until, max(busy_until, until), "child sick (VAB)"))
    released = {v for v in _future_visits_of(plan, eid, clock)
                if plan.assignments[v].start < until}
    for v in released:
        world.scenario.visits[v].locked = False
    ret = f", back {fmt_time(until)}" if back and until < emp.shift_end else " for the rest of the day"
    return IncidentEffect(
        kind=inc.kind,
        clock=clock,
        description=f"{eid}'s child is sick at {fmt_time(clock)}: {eid} leaves after {doing.split(' until')[0].replace('finishing ', '')} "
                    f"({fmt_time(busy_until)}) to pick up the child{ret}.",
        affected_employees={eid} | _partners(plan, released),
        released_visits=released,
        facts={"employee": eid, "leaves_at": busy_until, "back_at": until if back else None,
               "future_visits_affected": len(released)},
    )


def _alarm(world: World, plan: Plan, inc: Incident) -> IncidentEffect:
    """Safety alarm (trygghetslarm): a new urgent visit; the employee who can be
    there first is dispatched and their later visits are re-planned."""
    sc = world.scenario
    clock = inc.clock
    rid = inc.params["recipient_id"]
    duration = int(inc.params.get("duration", 20))
    max_response = int(inc.params.get("max_response_minutes", 45))
    r = sc.recipients[rid]
    for x, a in plan.assignments.items():
        if sc.visits.get(x) and sc.visits[x].recipient_id == rid and a.start <= clock < a.end + 10:
            raise ValueError(f"{rid} has {x} in progress ({', '.join(a.employee_ids)} until {fmt_time(a.end)}): "
                             "the staff on site answer the alarm")
    n = sum(1 for k in sc.visits if k.startswith("ALARM-")) + 1
    vid = f"ALARM-{n:02d}"
    iid = f"I-ALARM-{n:02d}"
    sc.interventions[iid] = Intervention(
        id=iid, recipient_id=rid, type="alarm_response", duration_minutes=duration, required_skills=[],
        required_delegations=[], priority=5, timing=TimingKind.HARD, preferred_time=clock,
        earliest_start=clock, latest_start=clock + max_response, requires_double_staffing=False,
        notes="Safety alarm response", source={"system": "trygghetslarm", "synthetic": True},
    )
    v = _make_visit(vid, r, [sc.interventions[iid]])
    v.slot = "alarm"
    sc.visits[vid] = v
    # Fastest responder among employees on shift who may visit this recipient.
    from .diagnostics import _qualifies

    tm = world.travel()
    ranked = []
    for eid, emp in sc.employees.items():
        if emp.status != EmployeeStatus.WORKING or emp.shift_end < clock + duration or emp.shift_start > clock + max_response:
            continue
        if _qualifies(sc, eid, v, 0):
            continue
        loc, free, doing = _position_at(world, plan, eid, clock)
        arrive = free + tm.for_employee(emp).minutes(loc, r.location_id)
        if arrive > clock + max_response or arrive + duration > emp.shift_end:
            continue
        # Not someone who is (about to be) unavailable: sick, VAB, split-shift gap, break window closing.
        if any(iv.start < arrive + duration + 30 and iv.end > free for iv in emp.unavailable):
            continue
        ranked.append(((arrive, eid), eid, loc, free, doing))
    ranked.sort()
    best = ranked[0] if ranked else None
    if best is None:
        return IncidentEffect(
            kind=inc.kind, clock=clock,
            description=f"Safety alarm from {rid} at {fmt_time(clock)}: nobody can be there within {max_response} min.",
            released_visits={vid}, zones={r.zone}, facts={"alarm_visit": vid, "dispatched": None},
        )
    (arrive, eid), _, loc, free, doing = best
    # Everything the dispatched employee had from the alarm on, and any visit of the
    # same recipient overlapping the alarm, is re-planned.
    released = {x for x in _future_visits_of(plan, eid, clock) if plan.assignments[x].start < 24 * 60}
    for x, a in plan.assignments.items():
        if sc.visits.get(x) and sc.visits[x].recipient_id == rid and a.start < arrive + duration and a.end > arrive:
            released.add(x)
    released.add(vid)
    for x in released:
        if x in sc.visits:
            sc.visits[x].locked = False
    return IncidentEffect(
        kind=inc.kind,
        clock=clock,
        description=(f"Safety alarm from {rid} ({r.address}) at {fmt_time(clock)}: {eid} dispatched ({doing}), "
                     f"arrives {fmt_time(arrive)}, {arrive - clock} min after the alarm."),
        affected_employees={eid} | _partners(plan, released - {vid}),
        released_visits=released,
        zones={r.zone},
        facts={"alarm_visit": vid, "dispatched": eid, "arrive": arrive, "response_minutes": arrive - clock,
               "pin": {vid: [eid, arrive]}, "employee_was": doing,
               # Fallbacks if the first responder's day cannot absorb the alarm.
               "responders": [[e, k[0], d] for k, e, _, _, d in ranked[:5]]},
    )
