import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notana_planner.domain import ScenarioConfig  # noqa: E402
from notana_planner.generator import generate_scenario  # noqa: E402
from notana_planner.planner import World, plan_day  # noqa: E402

SMALL = dict(seed=11, target_interventions=900, employee_count=20)


@pytest.fixture(scope="session")
def small_world():
    return World.create(generate_scenario(ScenarioConfig(**SMALL)))


@pytest.fixture(scope="session")
def small_plan(small_world):
    return plan_day(small_world, "baseline", 6)


@pytest.fixture
def plan_copy(small_plan):
    return copy.deepcopy(small_plan)
