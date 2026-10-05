"""Deterministic synthetic scenario generator.

The generator produces *structured interventions* (as they would exist in the
Notana Care database after the implementation plan has been structured),
synthetic care recipients and employees with shifts. Interventions are then
grouped into visits by :mod:`visit_builder` -- the generator never creates
visits directly.

The same ``ScenarioConfig`` (incl. seed) always produces exactly the same
scenario: only a seeded ``random.Random`` is used and iteration order is fixed.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace

from . import catalog as C
from .domain import (
    BreakRule,
    CareRecipient,
    Employee,
    Intervention,
    Interval,
    Location,
    Scenario,
    ScenarioConfig,
    TimingKind,
)
from .geo import AreaPreset, Cluster, get_area
from .visit_builder import build_visits

FIRST_NAMES = (
    "Astrid", "Birgitta", "Karin", "Margareta", "Ingrid", "Gunnel", "Elsa", "Maj", "Signe", "Ulla",
    "Göran", "Bengt", "Lennart", "Sven", "Kjell", "Rolf", "Arne", "Stig", "Bo", "Gösta",
    "Elisabeth", "Anna", "Eva", "Kerstin", "Marianne", "Inga", "Sonja", "Barbro", "Gun", "Britt",
    "Lars", "Per", "Olof", "Nils", "Erik", "Karl", "Hans", "Åke", "Leif", "Ingvar",
)
LAST_NAMES = (
    "Andersson", "Johansson", "Karlsson", "Nilsson", "Eriksson", "Larsson", "Olsson", "Persson",
    "Svensson", "Gustafsson", "Pettersson", "Jonsson", "Jansson", "Hansson", "Bengtsson", "Lindberg",
    "Lindqvist", "Berglund", "Sandberg", "Forsberg", "Sjöberg", "Wallin", "Engström", "Danielsson",
    "Holm", "Lundgren", "Björk", "Nyström", "Hedlund", "Åberg",
)
STAFF_FIRST = (
    "Amina", "Sara", "Fatima", "Johanna", "Linnea", "Maria", "Elin", "Hanna", "Leyla", "Noor",
    "Ali", "Ahmed", "Johan", "Mikael", "Daniel", "Omar", "Erik", "Jonas", "Yusuf", "Marko",
    "Sofia", "Emma", "Ida", "Maja", "Lejla", "Aisha", "Klara", "Tove", "Rana", "Selam",
    "Viktor", "Oskar", "Samir", "Hassan", "Emil", "Filip", "Adam", "Isak", "Tekle", "Matti",
)

# (name, start, end, unpaid gap, meal-break window, weight at pressure 0, weight at pressure 1)
# "split" is the Swedish home-care "delad tur": morning + evening with a gap.
# Meal-break windows sit between the demand peaks (morning / midday / evening).
SHIFT_TEMPLATES = (
    ("early", 6 * 60 + 30, 15 * 60, None, (10 * 60 + 15, 11 * 60 + 30), 0.36, 0.30),
    ("day", 8 * 60 + 30, 17 * 60, None, (13 * 60, 14 * 60), 0.12, 0.08),
    ("split", 7 * 60, 22 * 60 + 15, (12 * 60 + 15, 17 * 60), None, 0.18, 0.10),
    ("late", 13 * 60 + 30, 22 * 60 + 15, None, (16 * 60 + 45, 17 * 60 + 45), 0.26, 0.22),
    ("morning-part", 7 * 60, 11 * 60 + 30, None, None, 0.02, 0.12),
    ("evening-part", 16 * 60 + 45, 22 * 60 + 15, None, None, 0.06, 0.18),
)


def _quota(weights: list[float], total: int) -> list[int]:
    """Largest-remainder allocation of ``total`` items to ``weights``."""
    s = sum(weights)
    raw = [w / s * total for w in weights]
    counts = [int(math.floor(r)) for r in raw]
    rest = total - sum(counts)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - counts[i]), i))
    for i in order[:rest]:
        counts[i] += 1
    return counts


def _point_near(rng: random.Random, cluster: Cluster) -> tuple[float, float]:
    # Gaussian scatter, truncated at 2 sigma; degrees per km at this latitude.
    sigma_km = cluster.radius_km / 1.6
    while True:
        dx, dy = rng.gauss(0, sigma_km), rng.gauss(0, sigma_km)
        if dx * dx + dy * dy <= (2 * sigma_km) ** 2:
            break
    dlat = dy / 111.32
    dlon = dx / (111.32 * math.cos(math.radians(cluster.lat)))
    return round(cluster.lat + dlat, 6), round(cluster.lon + dlon, 6)


def generate_scenario(config: ScenarioConfig | None = None, scenario_id: str | None = None) -> Scenario:
    cfg = replace(config) if config else ScenarioConfig()
    rng = random.Random(cfg.seed)
    area = get_area(cfg.area)

    locations: dict[str, Location] = {}
    bases = _make_bases(area, locations)

    recipients, interventions = _make_demand(rng, cfg, area, locations)
    employees = _make_employees(rng, cfg, area, recipients, bases)
    _assign_continuity(rng, recipients, employees)

    visits = build_visits(recipients, interventions)

    sid = scenario_id or f"S-{cfg.seed}-{cfg.area}-{cfg.employee_count}-{cfg.target_interventions}"
    return Scenario(
        id=sid,
        config=cfg,
        area_name=area.name,
        recipients=recipients,
        interventions=interventions,
        visits=visits,
        employees=employees,
        locations=locations,
        bases=bases,
    )


def _make_bases(area: AreaPreset, locations: dict[str, Location]) -> list[str]:
    bases = []
    for i, cl in enumerate(area.clusters):
        loc = Location(
            id=f"B-{i + 1:02d}",
            lat=round(cl.lat + 0.0012, 6),
            lon=round(cl.lon - 0.0015, 6),
            zone=cl.name,
            label=f"Team office {cl.name}",
        )
        locations[loc.id] = loc
        bases.append(loc.id)
    return bases


def _recipient_profile(rng: random.Random, cfg: ScenarioConfig) -> dict:
    # Recipients needing two-person transfers: about 70% of their visits are
    # double staffed, so pick enough of them to reach the requested share.
    double_share = min(1.0, cfg.double_staffing_pct / 0.7)
    return {
        "diabetic": rng.random() < 0.20,
        "dementia": rng.random() < 0.25,
        "transfer_two": rng.random() < double_share,
        "catheter": rng.random() < 0.10,
        "wound": rng.random() < 0.11,
        "finnish": rng.random() < 0.05,
        "palliative": rng.random() < 0.04,
        "eye_drops": rng.random() < 0.12,
        "wound_slot": rng.choice((C.M, C.D, C.A)),
        "visits": rng.choices((4, 3, 2), weights=(0.82, 0.12, 0.06))[0],
        "continuity_weight": round(rng.choice((0.6, 1.0, 1.0, 1.0, 1.5, 2.0)), 2),
    }


def _slots_for(n_visits: int) -> tuple[str, ...]:
    if n_visits >= 4:
        return C.SLOT_ORDER
    if n_visits == 3:
        return (C.M, C.D, C.E)
    return (C.M, C.E)


def _make_demand(
    rng: random.Random, cfg: ScenarioConfig, area: AreaPreset, locations: dict[str, Location]
) -> tuple[dict[str, CareRecipient], dict[str, Intervention]]:
    recipients: dict[str, CareRecipient] = {}
    interventions: dict[str, Intervention] = {}
    cluster_weights = [c.weight for c in area.clusters]
    strict_pct = cfg.strict_window_pct
    extra_skill_p = max(0.0, (cfg.skill_requirement_pct - strict_pct) / max(1e-6, 1 - strict_pct))
    int_counter = 0
    r_idx = 0
    while int_counter < cfg.target_interventions:
        r_idx += 1
        cluster = rng.choices(area.clusters, weights=cluster_weights)[0]
        lat, lon = _point_near(rng, cluster)
        rid = f"R-{r_idx:03d}"
        loc_id = f"L-{rid}"
        street = rng.choice(area.street_names)
        address = f"{street} {rng.randint(1, 140)}, {cluster.name}"
        locations[loc_id] = Location(loc_id, lat, lon, cluster.name, address)
        prof = _recipient_profile(rng, cfg)
        recipients[rid] = CareRecipient(
            id=rid,
            name=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            location_id=loc_id,
            lat=lat,
            lon=lon,
            address=address,
            zone=cluster.name,
            continuity_weight=prof["continuity_weight"],
        )
        slots = _slots_for(prof["visits"])
        for slot in slots:
            int_counter += _make_visit_demand(
                rng, cfg, rid, prof, slot, slots, extra_skill_p, interventions, int_counter
            )
    return recipients, interventions


def _make_visit_demand(
    rng: random.Random,
    cfg: ScenarioConfig,
    rid: str,
    prof: dict,
    slot: str,
    slots: tuple[str, ...],
    extra_skill_p: float,
    interventions: dict[str, Intervention],
    counter: int,
) -> int:
    sdef = C.SLOTS[slot]
    strict = rng.random() < cfg.strict_window_pct
    double = prof["transfer_two"] and rng.random() < 0.7

    # Candidate (non time-critical) interventions for the slot, filtered by profile.
    generic = []
    for t in C.INTERVENTION_TYPES:
        if slot not in t.slots or t.time_critical:
            continue
        if t.key == "catheter" and not prof["catheter"]:
            continue
        if t.key == "wound_care" and not (prof["wound"] and slot == prof["wound_slot"]):
            continue
        generic.append(t)
    critical: list[C.InterventionType] = []
    if strict:
        if prof["diabetic"] and slot in (C.M, C.E):
            critical.append(C.INTERVENTION_BY_KEY["insulin"])
        critical.append(C.INTERVENTION_BY_KEY["medication"])
        if prof["eye_drops"] and slot in (C.M, C.E):
            critical.append(C.INTERVENTION_BY_KEY["eye_drops"])

    lo, hi = sdef["count"]
    n_total = rng.randint(lo, hi)
    n_generic = max(1, min(len(generic), n_total - len(critical)))
    chosen = critical + rng.sample(generic, n_generic)
    if prof["wound"] and slot == prof["wound_slot"] and not any(t.key == "wound_care" for t in chosen):
        chosen.append(C.INTERVENTION_BY_KEY["wound_care"])  # wound care: once a day
    if double and not any(t.needs_transfer for t in chosen):
        chosen.append(C.INTERVENTION_BY_KEY["transfer"])

    durations = [rng.randint(t.min_duration, t.max_duration) for t in chosen]
    visit_duration = sum(durations)

    # Preferred start + windows. The visit must fit inside its slot so that
    # visits of one recipient never overlap.
    slot_start = sdef["start"]
    if slot == C.E and C.A not in slots:
        slot_start = 17 * 60
    slot_latest = sdef["end"] - visit_duration
    pref = rng.randrange(sdef["pref"][0], sdef["pref"][1] + 1, 5)
    pref = max(slot_start, min(slot_latest, pref))
    hard_half = rng.choice((15, 20, 30, 30, 45))
    soft_half = rng.choice((60, 75, 90))

    # Visit-level qualification requirements beyond delegations from critical tasks.
    skills: list[str] = []
    want_extra = (not strict and rng.random() < extra_skill_p) or (strict and rng.random() < 0.25)
    if double:
        skills.append(C.HOIST)
    if want_extra:
        if prof["dementia"]:
            skills.append(C.DEMENTIA)
        if prof["finnish"]:
            skills.append(C.FINNISH)
        if prof["palliative"]:
            skills.append(C.PALLIATIVE)
        if not skills:
            skills.append(rng.choice((C.DEMENTIA, C.HOIST)))

    made = 0
    for t, dur in zip(chosen, durations):
        made += 1
        iid = f"I-{counter + made:05d}"
        if t.time_critical:
            timing = TimingKind.HARD
            earliest = max(slot_start, pref - hard_half)
            latest = min(slot_latest, pref + hard_half)
        else:
            timing = TimingKind.SOFT
            earliest = max(slot_start, pref - soft_half)
            latest = min(slot_latest, pref + soft_half)
        task_skills = set(t.skills) | {sk for sk in skills if sk != C.HOIST}
        if C.HOIST in skills and t.needs_transfer:
            task_skills.add(C.HOIST)
        req_skills = sorted(task_skills)
        interventions[iid] = Intervention(
            id=iid,
            recipient_id=rid,
            type=t.key,
            duration_minutes=dur,
            required_skills=req_skills,
            required_delegations=list(t.delegations),
            priority=t.priority,
            timing=timing,
            preferred_time=pref,
            earliest_start=earliest,
            latest_start=latest,
            requires_double_staffing=double and t.needs_transfer,
            notes=t.label,
            source={
                "system": "notana-care",
                "origin": "implementation_plan",
                "implementation_plan_id": f"IP-{rid}",
                "slot": slot,
                "synthetic": True,
                "seed": cfg.seed,
            },
        )
    return made


def _make_employees(
    rng: random.Random,
    cfg: ScenarioConfig,
    area: AreaPreset,
    recipients: dict[str, CareRecipient],
    bases: list[str],
) -> dict[str, Employee]:
    n = cfg.employee_count
    p = max(0.0, min(1.0, cfg.staffing_pressure))
    weights = [w0 + (w1 - w0) * p for (*_, w0, w1) in SHIFT_TEMPLATES]
    shift_counts = _quota(weights, n)
    shift_list: list[tuple] = []
    for (name, s, e, gap, brk, _, _), cnt in zip(SHIFT_TEMPLATES, shift_counts):
        shift_list += [(name, s, e, gap, brk)] * cnt
    rng.shuffle(shift_list)

    # Teams proportional to recipient demand per zone.
    zone_names = [c.name for c in area.clusters]
    demand = [sum(1 for r in recipients.values() if r.zone == z) + 0.5 for z in zone_names]
    team_counts = _quota(demand, n)
    teams: list[int] = []
    for zi, cnt in enumerate(team_counts):
        teams += [zi] * cnt

    employees: dict[str, Employee] = {}
    for i in range(n):
        eid = f"E-{i + 1:03d}"
        shift_name, s, e, gap, brk = shift_list[i]
        zi = teams[i]
        q = C.EMPLOYEE_QUALIFICATION_RATES
        delegations = []
        if rng.random() < q[C.MEDICATION]:
            delegations.append(C.MEDICATION)
            if rng.random() < q[C.INSULIN]:
                delegations.append(C.INSULIN)
        for d in (C.WOUND_CARE, C.CATHETER):
            if rng.random() < q[d]:
                delegations.append(d)
        skills = [sk for sk in C.SKILLS if rng.random() < q[sk]]
        breaks = []
        unavailable = []
        if gap is not None:
            unavailable.append(Interval(gap[0], gap[1], "split-shift gap"))
        elif cfg.breaks_enabled and brk is not None:
            breaks.append(BreakRule(duration_minutes=30, earliest_start=brk[0], latest_start=brk[1]))
        employees[eid] = Employee(
            id=eid,
            name=f"{rng.choice(STAFF_FIRST)} {rng.choice(LAST_NAMES)[0]}.",
            team=zone_names[zi],
            shift_start=s,
            shift_end=e,
            start_location_id=bases[zi],
            end_location_id=bases[zi],
            skills=skills,
            delegations=delegations,
            breaks=breaks,
            unavailable=unavailable,
            max_workload_minutes=None,
        )
        employees[eid].name += f" ({shift_name})"
    return employees


def _assign_continuity(
    rng: random.Random, recipients: dict[str, CareRecipient], employees: dict[str, Employee]
) -> None:
    by_team: dict[str, list[str]] = {}
    for e in employees.values():
        by_team.setdefault(e.team, []).append(e.id)
    for r in recipients.values():
        team = by_team.get(r.zone) or list(employees)
        k = min(len(team), rng.randint(4, 7))
        known = rng.sample(team, k)
        r.known_employee_ids = sorted(known)
        if rng.random() < 0.3:
            r.preferred_employee_ids = sorted(rng.sample(known, min(len(known), rng.randint(1, 2))))
        others = [e for e in team if e not in known]
        if others and rng.random() < 0.04:
            r.avoid_employee_ids = [rng.choice(others)]
        for eid in known:
            employees[eid].known_recipient_ids.append(r.id)
