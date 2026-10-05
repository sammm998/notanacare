"""High-level planning facade: world state + global planning + finalisation.

``World`` holds the scenario together with the travel base matrix and the
currently active traffic conditions. Every plan produced through this module
is independently validated, diagnosed and scored before it is returned.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import ObjectiveWeights, SolverSettings
from .diagnostics import diagnose_unplanned
from .domain import Plan, Scenario
from .plan_builder import assemble_plan
from .scoring import score_plan
from .solver.builder import build_global_problem
from .solver.engine import SolveEngine, SolveOptions
from .travel import BaseMatrix, TrafficConditions, TravelMatrix, TravelTimeProvider, build_base_matrix, make_provider
from .validator import validate_plan

STRATEGIES = {
    "baseline": "A. Baseline deterministic optimizer (construction + OR-Tools GLS + CP-SAT timetabling)",
    "enhanced": "B. Optimizer + enhanced repair heuristic (ejection chains)",
    "advisor": "C. Optimizer + planning advisor (LLM if configured, else deterministic fallback)",
    "classifier": "D. Optimizer + decision classifier ranking repair alternatives",
}


@dataclass
class World:
    scenario: Scenario
    base_matrix: BaseMatrix
    traffic: TrafficConditions = field(default_factory=TrafficConditions)
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    settings: SolverSettings = field(default_factory=SolverSettings)
    _travel_cache: TravelMatrix | None = None

    @classmethod
    def create(
        cls,
        scenario: Scenario,
        provider: TravelTimeProvider | None = None,
        weights: ObjectiveWeights | None = None,
        settings: SolverSettings | None = None,
    ) -> "World":
        provider = provider or make_provider()
        base = build_base_matrix(provider, list(scenario.locations.values()))
        st = settings or SolverSettings()
        st.sync_tolerance_min = scenario.config.double_staffing_sync_tolerance
        return cls(scenario, base, TrafficConditions(), weights or ObjectiveWeights(), st)

    def travel(self) -> TravelMatrix:
        tc = self._travel_cache
        if tc is None or tc.traffic.to_dict() != self.traffic.to_dict():
            tc = TravelMatrix(
                self.base_matrix,
                self.scenario.locations,
                self.scenario.config.travel_time_multiplier,
                self.traffic.copy(),
            )
            self._travel_cache = tc
        return tc


def finalize_plan(world: World, plan: Plan, reference: Plan | None = None, clock: int | None = None) -> Plan:
    """Validate (independently), explain unplanned visits and score."""
    tm = world.travel()
    t0 = time.perf_counter()
    report = validate_plan(
        world.scenario,
        tm,
        plan,
        sync_tolerance=world.settings.sync_tolerance_min,
        max_overtime=world.settings.max_overtime_min,
        reference=reference,
        clock=clock,
    )
    plan.validation = report.to_dict()
    plan.validation["seconds"] = round(time.perf_counter() - t0, 3)
    plan.unplanned = diagnose_unplanned(world.scenario, tm, plan)
    plan.score = score_plan(world.scenario, tm, plan, world.weights, reference)
    plan.travel_source = tm.source
    return plan


def plan_day(
    world: World,
    strategy: str = "baseline",
    time_limit_s: float | None = None,
) -> Plan:
    """Global optimisation of the whole day (no previous plan)."""
    tm = world.travel()
    problem = build_global_problem(world.scenario, tm, world.weights, world.settings)
    opts = SolveOptions(
        time_limit_s=time_limit_s or world.settings.time_limit_s,
        enhanced_repair=strategy in ("enhanced", "advisor", "classifier"),
    )
    out = SolveEngine(problem, opts).run()
    plan = assemble_plan(world.scenario, tm, problem, out, strategy=strategy)
    return finalize_plan(world, plan)
