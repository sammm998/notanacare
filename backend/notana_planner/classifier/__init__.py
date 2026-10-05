"""Optional decision-classifier extension point (JEV-style).

No verified JEV SDK is available in this environment, so only the interface and
a deterministic mock exist. A real implementation must satisfy the same
contract: it may rank *already-validated* alternatives and classify incidents,
never alter or approve a plan.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Alternative:
    key: str
    valid: bool
    features: dict[str, float]


class DecisionClassifier(ABC):
    name = "abstract"

    @abstractmethod
    def classify_incident(self, facts: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def rank_alternatives(self, alternatives: list[Alternative]) -> list[tuple[str, float]]: ...


class MockDecisionClassifier(DecisionClassifier):
    """Transparent linear scorer; lower score = better. Invalid plans are never ranked."""

    name = "mock-linear"
    WEIGHTS = {
        "lost_high_priority": 1000.0,
        "lost": 300.0,
        "unplanned": 100.0,
        "visits_changed": 4.0,
        "employee_changes": 6.0,
        "time_change_minutes": 0.2,
        "travel_minutes": 0.05,
        "routes_changed": 2.0,
    }

    def classify_incident(self, facts: dict[str, Any]) -> dict[str, Any]:
        released = int(facts.get("released_visits", 0))
        high = int(facts.get("released_high_priority", 0))
        urgency = "high" if high or released > 10 else "medium" if released > 3 else "low"
        return {
            "classifier": self.name,
            "urgency": urgency,
            "needs_manual_review": bool(high >= 3 or facts.get("incident_kind") == "medication_moved"),
            "recommended_start_phase": 2 if released > 15 else 1,
        }

    def rank_alternatives(self, alternatives: list[Alternative]) -> list[tuple[str, float]]:
        scored = []
        for a in alternatives:
            if not a.valid:
                continue
            scored.append((a.key, sum(self.WEIGHTS.get(k, 0.0) * v for k, v in a.features.items())))
        return sorted(scored, key=lambda x: x[1])


def make_classifier() -> DecisionClassifier:
    return MockDecisionClassifier()
