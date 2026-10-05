"""Objective weights and solver settings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields


@dataclass(slots=True)
class ObjectiveWeights:
    """Weights of the *soft* objectives. Hard constraints are never weighted.

    Costs are integer "points". Dropping a visit is priced so that it
    dominates every other term: the solver only leaves a visit unplanned if
    no feasible insertion exists (or it cannot find one within the limit).
    """

    unplanned_visit: int = 200_000  # per employee-slot of an unplanned visit
    priority_multiplier: float = 0.6  # penalty *= 1 + multiplier * (priority - 1)
    travel_minute: int = 20
    travel_km: int = 2
    preferred_time_minute: int = 3  # per minute away from preferred start
    continuity_unknown_employee: int = 150  # employee has never visited the recipient
    continuity_preferred_bonus: int = 120  # (as a penalty for NOT using a preferred employee)
    workload_overload_minute: int = 4  # minute of care above fair share
    overtime_minute: int = 400
    idle_minute: int = 1  # route span (start..end), compresses idle gaps
    # Re-planning stability
    employee_change: int = 900  # per visit-slot moved to another employee
    time_change_minute: int = 12  # per minute away from previously communicated start
    unaffected_route_touch: int = 300  # extra per visit moved off an unaffected route

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "ObjectiveWeights":
        w = cls()
        if data:
            names = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in names and v is not None:
                    setattr(w, k, type(getattr(w, k))(v))
        return w


@dataclass(slots=True)
class SolverSettings:
    time_limit_s: float = 25.0
    first_solution: str = "LOCAL_CHEAPEST_INSERTION"
    metaheuristic: str = "GUIDED_LOCAL_SEARCH"
    sync_tolerance_min: int = 0
    max_overtime_min: int = 0  # approved overtime that extends the hard shift end
    use_cpsat_timetabling: bool = True
    post_insertion: bool = True  # deterministic insertion pass for leftovers
    log_search: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
