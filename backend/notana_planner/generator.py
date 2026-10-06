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

import copy
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
    Visit,
    WEEKDAYS,
    WorkingTimeRules,
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
MALE_FIRST = frozenset(FIRST_NAMES[10:20] + FIRST_NAMES[30:40])
STAFF_FEMALE = (
    "Amina", "Sara", "Fatima", "Johanna", "Linnea", "Maria", "Elin", "Hanna", "Leyla", "Noor",
    "Sofia", "Emma", "Ida", "Maja", "Lejla", "Aisha", "Klara", "Tove", "Rana", "Selam",
)
STAFF_MALE = (
    "Ali", "Ahmed", "Johan", "Mikael", "Daniel", "Omar", "Erik", "Jonas", "Yusuf", "Marko",
    "Viktor", "Oskar", "Samir", "Hassan", "Emil", "Filip", "Adam", "Isak", "Tekle", "Matti",
)
# Languages besides Swedish (weights): recipients' mother tongues and staff languages.
RECIPIENT_LANGS = (("ar", 0.26), ("fa", 0.16), ("so", 0.14), ("bcs", 0.16), ("es", 0.12), ("pl", 0.16))
STAFF_LANGS = (("ar", 0.24), ("so", 0.14), ("fa", 0.12), ("bcs", 0.12), ("pl", 0.10), ("es", 0.08), ("en", 0.20))
SHARE_MALE_STAFF = 0.20  # Swedish home care is ~80 % women
SHARE_BIKE = 0.12  # staff without a driving licence: bike / public transport

# (name, start, end, unpaid gap, meal-break window, weight at pressure 0, weight at pressure 1)
# "split" is the Swedish home-care "delad tur": morning + evening with a gap.
# Meal-break windows sit between the demand peaks (morning / midday / evening).
# All templates comply with Arbetstidslagen: no stretch longer than 5 h without
# a rest, at most 10 h of work, and a span that leaves 11 h of dygnsvila.
SHIFT_TEMPLATES = (
    ("early", 6 * 60 + 30, 15 * 60, None, (10 * 60 + 15, 11 * 60 + 30), 0.38, 0.32),
    ("split", 7 * 60 + 30, 20 * 60 + 30, (12 * 60, 16 * 60 + 30), None, 0.20, 0.14),
    ("late", 13 * 60 + 30, 22 * 60 + 15, None, (16 * 60 + 45, 17 * 60 + 45), 0.28, 0.22),
    ("morning-part", 7 * 60, 11 * 60 + 30, None, None, 0.02, 0.12),
    ("evening-part", 17 * 60 + 15, 22 * 60 + 15, None, None, 0.06, 0.18),
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
    # Separate stream for person attributes and wishes, so they can be switched
    # off without changing demand, shifts or qualifications.
    prng = random.Random(cfg.seed * 1_000_003 + 17)
    area = get_area(cfg.area)

    locations: dict[str, Location] = {}
    bases = _make_bases(area, locations)

    recipients, interventions = _make_demand(rng, cfg, area, locations, prng)
    employees = _make_employees(rng, cfg, area, recipients, bases, prng)
    _assign_continuity(rng, recipients, employees)

    visits = build_visits(recipients, interventions)
    if cfg.preferences_enabled:
        _ensure_wish_cover(prng, recipients, employees, visits)

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


# Weekly frequencies (times per week) of interventions that are not daily.
WEEKLY_FREQUENCY = {"shower": 2, "cleaning": 1, "laundry": 1, "shopping": 2, "walk": 3}


def _weekly_days(seed: int, rid: str, key: str) -> set[int]:
    k = WEEKLY_FREQUENCY[key]
    r = random.Random(f"{seed}-{rid}-{key}")
    first = r.randrange(7)
    return {(first + round(i * 7 / k)) % 7 for i in range(k)}  # spread over the week


def generate_week(config: ScenarioConfig | None = None, scenario_id: str | None = None) -> list[Scenario]:
    """Seven day scenarios (Mon..Sun) for the same recipients and staff.

    * ``employee_count`` is the staff on duty per day; the head count is
      ``employee_count * 7 / 5``. Everyone keeps one shift type all week and
      has two consecutive days off (veckovila >= 48 h), so the roster itself
      complies with Arbetstidslagen; overtime is capped per day so that 11 h
      dygnsvila towards the next shift always remains.
    * Demand: same recipients and needs every day, new day-to-day variation;
      shower, cleaning, laundry, shopping and walks follow weekly frequencies.
    """
    cfg = replace(config) if config else ScenarioConfig()
    cfg.days = 7
    rng = random.Random(cfg.seed)
    prng = random.Random(cfg.seed * 1_000_003 + 17)
    area = get_area(cfg.area)
    locations: dict[str, Location] = {}
    bases = _make_bases(area, locations)
    weekly = {}

    def allowed_on(day: int):
        def allowed(rid: str, key: str) -> bool:
            if key not in WEEKLY_FREQUENCY:
                return True
            if (rid, key) not in weekly:
                weekly[(rid, key)] = _weekly_days(cfg.seed, rid, key)
            return day in weekly[(rid, key)]
        return allowed

    profiles: dict[str, dict] = {}
    recipients, day0 = _make_demand(rng, cfg, area, locations, prng, allowed_on(0), profiles)
    headcount = max(cfg.employee_count, round(cfg.employee_count * 7 / 5))
    staff_cfg = replace(cfg, employee_count=headcount)
    employees = _make_employees(rng, staff_cfg, area, recipients, bases, prng)
    _assign_continuity(rng, recipients, employees)
    # Days off spread evenly per shift type and team, so every day has the same mix.
    for i, e in enumerate(sorted(employees.values(), key=lambda e: (e.shift_name, e.team, e.id))):
        off = i % 7
        e.days_off = sorted({off, (off + 1) % 7})
        daily = (e.shift_end - e.shift_start) - sum(iv.end - iv.start for iv in e.unavailable) - sum(
            b.duration_minutes for b in e.breaks
        )
        e.contract_minutes_per_week = 5 * daily

    demand = [day0] + [_day_demand(random.Random(cfg.seed * 7 + d), cfg, recipients, profiles, allowed_on(d))
                       for d in range(1, 7)]
    sid = scenario_id or f"W-{cfg.seed}-{cfg.area}-{cfg.employee_count}-{cfg.target_interventions}"
    days_visits = [build_visits(recipients, ivs) for ivs in demand]
    if cfg.preferences_enabled:
        for d, visits in enumerate(days_visits):
            on_duty = {k: e for k, e in employees.items() if d not in e.days_off}
            _ensure_wish_cover(prng, recipients, on_duty, visits)

    scenarios = []
    rules = WorkingTimeRules()
    for d in range(7):
        staff = {}
        for k, e in employees.items():
            if d in e.days_off:
                continue
            ed = copy.deepcopy(e)
            if d < 6 and (d + 1) not in e.days_off:
                # Tomorrow's shift starts at the same time: 11 h dygnsvila before it.
                ed.latest_end_by_rest = e.shift_start + 24 * 60 - rules.min_daily_rest_min
            staff[k] = ed
        scenarios.append(
            Scenario(
                id=f"{sid}-{WEEKDAYS[d].lower()}",
                config=replace(cfg),
                area_name=area.name,
                recipients=copy.deepcopy(recipients),
                interventions=demand[d],
                visits=days_visits[d],
                employees=staff,
                locations=locations,
                bases=bases,
                day=d,
                rules=replace(rules),
            )
        )
    return scenarios


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
    rng: random.Random,
    cfg: ScenarioConfig,
    area: AreaPreset,
    locations: dict[str, Location],
    prng: random.Random,
    allowed=None,  # (recipient id, intervention type) -> bool; weekly frequencies
    profiles: dict[str, dict] | None = None,
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
        first = rng.choice(FIRST_NAMES)
        recipients[rid] = CareRecipient(
            id=rid,
            name=f"{first} {rng.choice(LAST_NAMES)}",
            gender="M" if first in MALE_FIRST else "F",
            location_id=loc_id,
            lat=lat,
            lon=lon,
            address=address,
            zone=cluster.name,
            continuity_weight=prof["continuity_weight"],
        )
        if cfg.preferences_enabled:
            _recipient_wishes(prng, recipients[rid], prof)
        if profiles is not None:
            profiles[rid] = prof
        slots = _slots_for(prof["visits"])
        for slot in slots:
            int_counter += _make_visit_demand(
                rng, cfg, rid, prof, slot, slots, extra_skill_p, interventions, int_counter, allowed
            )
    return recipients, interventions


def _day_demand(
    rng: random.Random, cfg: ScenarioConfig, recipients: dict[str, CareRecipient], profiles: dict[str, dict], allowed
) -> dict[str, Intervention]:
    """Another day for the same recipients: same needs, new day-to-day variation."""
    strict_pct = cfg.strict_window_pct
    extra_skill_p = max(0.0, (cfg.skill_requirement_pct - strict_pct) / max(1e-6, 1 - strict_pct))
    interventions: dict[str, Intervention] = {}
    counter = 0
    for rid in recipients:
        prof = profiles[rid]
        slots = _slots_for(prof["visits"])
        for slot in slots:
            counter += _make_visit_demand(
                rng, cfg, rid, prof, slot, slots, extra_skill_p, interventions, counter, allowed
            )
    return interventions


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
    allowed=None,
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
        if allowed is not None and not allowed(rid, t.key):
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
        if prof["palliative"]:
            skills.append(C.PALLIATIVE)
        if not skills and not prof["finnish"]:
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
    prng: random.Random,
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
        finnish = rng.random() < 0.08  # Finnish-speaking staff
        breaks = []
        unavailable = []
        if gap is not None:
            unavailable.append(Interval(gap[0], gap[1], "split-shift gap"))
        elif cfg.breaks_enabled and brk is not None:
            breaks.append(BreakRule(duration_minutes=30, earliest_start=brk[0], latest_start=brk[1]))
        gender = "M" if prng.random() < SHARE_MALE_STAFF else "F"
        first = prng.choice(STAFF_MALE if gender == "M" else STAFF_FEMALE)
        employees[eid] = Employee(
            id=eid,
            name=f"{first} {rng.choice(LAST_NAMES)[0]}.",
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
            gender=gender,
            languages=["sv", "fi"] if finnish else ["sv"],
            shift_name=shift_name,
        )
        if cfg.preferences_enabled:
            _employee_attributes(prng, employees[eid])
        employees[eid].name += f" ({shift_name})"
    return employees


def _weighted(rng: random.Random, pairs: tuple[tuple[str, float], ...]) -> str:
    return rng.choices([k for k, _ in pairs], weights=[w for _, w in pairs])[0]


def _recipient_wishes(prng: random.Random, r: CareRecipient, prof: dict) -> None:
    """Who may / should come: gender, language, pets, smoking."""
    if prng.random() < 0.22:
        r.gender_preference = "F" if (r.gender == "F" and prng.random() < 0.95) or prng.random() < 0.6 else "M"
        r.gender_strict = prng.random() < 0.40
        r.gender_scope = "intimate" if prng.random() < 0.75 else "all"
    if prof["finnish"]:
        r.languages = ["fi"]
    elif prng.random() < 0.07:
        r.languages = [_weighted(prng, RECIPIENT_LANGS)]
    if r.languages:
        # Dementia often means losing the second language: then a hard requirement (lead).
        r.language_required = prng.random() < (0.6 if prof["dementia"] else 0.15)
    x = prng.random()
    r.pets = ["dog"] if x < 0.14 else ["cat"] if x < 0.24 else ["dog", "cat"] if x < 0.25 else []
    r.smokes = prng.random() < 0.08


def _employee_attributes(prng: random.Random, e: Employee) -> None:
    if prng.random() < 0.30:
        lang = _weighted(prng, STAFF_LANGS)
        if lang not in e.languages:
            e.languages.append(lang)
    x = prng.random()
    e.pet_allergies = ["cat"] if x < 0.05 else ["dog"] if x < 0.075 else ["cat", "dog"] if x < 0.08 else []
    e.avoid_smoking = prng.random() < 0.12
    e.travel_mode = "bike" if prng.random() < SHARE_BIKE else "car"
    if prng.random() < 0.30:
        e.preferred_zones = [e.team]


def _ensure_wish_cover(
    prng: random.Random,
    recipients: dict[str, CareRecipient],
    employees: dict[str, Employee],
    visits: dict[str, Visit],
) -> None:
    """Keep hard wishes coherent with the roster, like a unit manager would.

    * Required language: for every visit of such a recipient, at least two
      employees of the team who are on shift and hold the visit's delegations
      speak the language (the unit staffs its shifts accordingly).
    * Strict gender: if fewer than two such employees of the wished gender are
      on shift for a visit, the unit cannot promise it and it becomes a wish.
    """
    def fits(e: Employee, v: Visit) -> bool:
        if e.shift_start > v.latest_start or e.shift_end < v.earliest_start + v.total_duration_minutes:
            return False
        if any(iv.start <= v.earliest_start and iv.end >= v.latest_start for iv in e.unavailable):
            return False
        return all(d in e.delegations for d in v.required_delegations) and all(s in e.skills for s in v.required_skills)

    for v in sorted(visits.values(), key=lambda x: x.id):
        r = recipients[v.recipient_id]
        team = [e for e in sorted(employees.values(), key=lambda e: e.id) if e.team == r.zone and fits(e, v)]
        if r.language_required:
            speakers = [e for e in team if set(r.languages) & set(e.languages)]
            others = [e for e in team if e not in speakers]
            prng.shuffle(others)
            for e in others[: max(0, 2 - len(speakers))]:
                e.languages.append(r.languages[0])
        if r.gender_strict and (r.gender_scope == "all" or v.intimate_care):
            if sum(1 for e in team if e.gender == r.gender_preference) < 2:
                r.gender_strict = False


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
