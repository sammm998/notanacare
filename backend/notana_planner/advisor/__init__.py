"""Optional planning-advisor layer.

The advisor may *suggest* how to search (which repair phase to start with,
neighbourhood size, horizon, time budgets) and may *narrate* structured facts.
It never decides feasibility: every suggestion is clamped to safe bounds and
executed by the deterministic engine, and every resulting plan is checked by
the independent validator.
"""

from __future__ import annotations

import os

from .base import AdvisorDecision, PlanningAdvisor
from .deterministic import DeterministicAdvisor


def make_advisor(prefer_llm: bool = True) -> PlanningAdvisor:
    if prefer_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from .claude import ClaudePlanningAdvisor

            return ClaudePlanningAdvisor()
        except Exception:  # noqa: BLE001 - SDK missing etc.
            pass
    return DeterministicAdvisor()


__all__ = ["AdvisorDecision", "PlanningAdvisor", "DeterministicAdvisor", "make_advisor"]
