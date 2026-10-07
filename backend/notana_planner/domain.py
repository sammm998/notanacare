"""Typed domain model for the Notana Care planning simulator.

All times are integer minutes since local midnight of the planning day.
All people and addresses are synthetic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TimingKind(str, Enum):
    HARD = "hard"  # earliest/latest start are hard constraints
    SOFT = "soft"  # only a preferred time; window is generous


class VisitStatus(str, Enum):
    PLANNED = "planned"
    UNPLANNED = "unplanned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EmployeeStatus(str, Enum):
    WORKING = "working"
    SICK = "sick"
    OFF = "off"


@dataclass(slots=True)
class Location:
    id: str
    lat: float
    lon: float
    zone: str
    label: str = ""


@dataclass(slots=True)
class CareRecipient:
    id: str
    name: str
    location_id: str
    lat: float
    lon: float
    address: str
    zone: str
    continuity_weight: float = 1.0  # how much continuity matters for this person
    gender: str = "F"
    preferred_employee_ids: list[str] = field(default_factory=list)
    avoid_employee_ids: list[str] = field(default_factory=list)  # hard exclusion
    known_employee_ids: list[str] = field(default_factory=list)  # continuity history
    # Preferences about who comes. gender_preference: None | "F" | "M";
    # gender_strict: hard requirement (else a weighted wish); gender_scope:
    # "intimate" (hygiene, shower, toileting, dressing...) or "all" visits.
    gender_preference: str | None = None
    gender_strict: bool = False
    gender_scope: str = "intimate"
    languages: list[str] = field(default_factory=list)  # preferred languages besides Swedish
    language_required: bool = False  # e.g. dementia + mother tongue: hard requirement
    pets: list[str] = field(default_factory=list)  # "dog" | "cat"
    smokes: bool = False


@dataclass(slots=True)
class Intervention:
    id: str
    recipient_id: str
    type: str
    duration_minutes: int
    required_skills: list[str]
    required_delegations: list[str]
    priority: int  # 1 (low) .. 5 (critical / medical)
    timing: TimingKind
    preferred_time: int
    earliest_start: int
    latest_start: int
    requires_double_staffing: bool = False
    notes: str = ""
    source: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Visit:
    id: str
    recipient_id: str
    intervention_ids: list[str]
    total_duration_minutes: int
    location_id: str
    earliest_start: int
    latest_start: int
    preferred_start: int
    timing: TimingKind
    required_skills: list[str]
    required_delegations: list[str]
    required_employee_count: int
    priority: int
    continuity_preference: float
    locked: bool = False
    status: VisitStatus = VisitStatus.UNPLANNED
    slot: str = ""  # morning / midday / afternoon / evening (generation metadata)
    intimate_care: bool = False  # contains hygiene / shower / toileting / dressing ...


@dataclass(slots=True)
class BreakRule:
    duration_minutes: int
    earliest_start: int
    latest_start: int
    kind: str = "break"  # "break" (meal break) | "unavailable" (fixed gap, e.g. split shift)


@dataclass(slots=True)
class Interval:
    start: int
    end: int
    reason: str = ""


@dataclass(slots=True)
class Employee:
    id: str
    name: str
    team: str
    shift_start: int
    shift_end: int
    start_location_id: str
    end_location_id: str
    skills: list[str]
    delegations: list[str]
    status: EmployeeStatus = EmployeeStatus.WORKING
    breaks: list[BreakRule] = field(default_factory=list)
    unavailable: list[Interval] = field(default_factory=list)
    known_recipient_ids: list[str] = field(default_factory=list)
    max_workload_minutes: int | None = None
    # Real-time state used when re-planning: earliest time the employee can
    # start the next not-yet-started visit, and where they are at that time.
    available_from: int | None = None
    current_location_id: str | None = None
    # Person attributes and wishes
    gender: str = "F"  # "F" | "M"
    languages: list[str] = field(default_factory=lambda: ["sv"])
    pet_allergies: list[str] = field(default_factory=list)  # cannot work where these pets live
    avoid_smoking: bool = False  # work-environment: no smoking homes
    travel_mode: str = "car"  # "car" | "bike" (does not drive: bike / public transport)
    preferred_zones: list[str] = field(default_factory=list)  # wish, weighted
    contract_minutes_per_week: int = 2400  # sysselsättningsgrad (40 h = full time)
    latest_end_by_rest: int | None = None  # week planning: tomorrow's start - 11 h dygnsvila
    days_off: list[int] = field(default_factory=list)  # week roster: 0 = Monday
    shift_name: str = ""
    # Home and commute to the team office (staff register, see staff_homes.py); information only
    home_address: str = ""
    home_district: str = ""
    home_lat: float | None = None
    home_lon: float | None = None
    commute_minutes: int | None = None
    commute_km: float | None = None
    commute_mode: str = ""  # "car" | "transit"
    commute_source: str = ""  # "google" | "estimate"

    @property
    def travel_factor(self) -> float:
        return BIKE_TRAVEL_FACTOR if self.travel_mode == "bike" else 1.0


# Door-to-door travel time relative to a car for employees who do not drive
# (bike / public transport in a dense Swedish city; parking overhead is lower).
BIKE_TRAVEL_FACTOR = 1.35

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(slots=True)
class WorkingTimeRules:
    """Arbetstidslagen (ATL) + typical collective-agreement limits. All hard."""

    enabled: bool = True
    max_continuous_work_min: int = 300  # ATL 15 §: no more than 5 h in a row without a break
    max_daily_work_min: int = 600  # collective agreement: max 10 h paid work per day
    min_daily_rest_min: int = 660  # ATL 13 §: 11 h dygnsvila per 24 h
    min_weekly_rest_min: int = 2160  # ATL 14 §: 36 h veckovila per 7 days
    max_weekly_overtime_min: int = 300  # on top of the contracted weekly hours


@dataclass(slots=True)
class ScenarioConfig:
    seed: int = 42
    target_interventions: int = 5000
    employee_count: int = 100
    area: str = "stockholm"
    staffing_pressure: float = 0.15  # 0 = generous full-time mix, 1 = many part-time shifts
    double_staffing_pct: float = 0.12
    strict_window_pct: float = 0.30
    skill_requirement_pct: float = 0.55
    travel_time_multiplier: float = 1.0
    double_staffing_sync_tolerance: int = 0  # minutes
    breaks_enabled: bool = True
    days: int = 1  # 1 = one day, 7 = a week (employee_count = staff on duty per day)
    preferences_enabled: bool = True  # gender / language / pets / smoking / travel mode


@dataclass(slots=True)
class Scenario:
    id: str
    config: ScenarioConfig
    area_name: str
    recipients: dict[str, CareRecipient]
    interventions: dict[str, Intervention]
    visits: dict[str, Visit]
    employees: dict[str, Employee]
    locations: dict[str, Location]
    bases: list[str]  # location ids of team offices
    day: int = 0  # 0 = Monday
    rules: WorkingTimeRules = field(default_factory=WorkingTimeRules)

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.day % 7]


# --------------------------------------------------------------------------
# Plan model
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RouteStop:
    """One item of an employee's day: a visit, a break or an unavailable gap."""

    kind: str  # "visit" | "break" | "unavailable"
    visit_id: str | None
    role: int  # visit role (0 = lead) or break index
    location_id: str
    start: int
    end: int
    travel_from_prev: int
    prev_location_id: str
    locked: bool = False


@dataclass(slots=True)
class Route:
    employee_id: str
    stops: list[RouteStop] = field(default_factory=list)
    route_start: int | None = None  # leaves start location
    route_end: int | None = None  # arrives at end location
    start_location_id: str = ""
    end_location_id: str = ""
    travel_to_end: int = 0

    def visit_stops(self) -> list[RouteStop]:
        return [s for s in self.stops if s.kind == "visit"]


@dataclass(slots=True)
class Assignment:
    visit_id: str
    employee_ids: list[str]
    start: int
    end: int
    starts: dict[str, int] = field(default_factory=dict)  # per employee start


@dataclass(slots=True)
class UnplannedReason:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UnplannedVisit:
    visit_id: str
    reasons: list[UnplannedReason]


@dataclass(slots=True)
class Plan:
    id: str
    scenario_id: str
    created_at: str
    strategy: str
    assignments: dict[str, Assignment]
    routes: dict[str, Route]
    unplanned: dict[str, UnplannedVisit]
    locked_visit_ids: list[str] = field(default_factory=list)
    clock: int | None = None  # simulation time the plan was made at
    solver_stats: dict[str, Any] = field(default_factory=dict)
    score: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    travel_source: str = "synthetic"
    parent_plan_id: str | None = None
    day: int = 0
    # Rule deviations a planner explicitly approved (see suggestions.apply_relaxed).
    exceptions: list[dict[str, Any]] = field(default_factory=list)


def to_jsonable(obj: Any) -> Any:
    """Convert dataclasses / enums (recursively) into JSON-friendly values."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def fmt_time(minutes: int | None) -> str:
    if minutes is None:
        return "--:--"
    sign = "-" if minutes < 0 else ""
    m = abs(int(minutes))
    return f"{sign}{m // 60:02d}:{m % 60:02d}"
