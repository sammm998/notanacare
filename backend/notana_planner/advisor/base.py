"""PlanningAdvisor interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Safe bounds for anything an advisor may suggest.
BOUNDS = {
    "start_phase": (1, 3),
    "phase1_helpers": (2, 20),
    "phase1_horizon_min": (60, 480),
    "phase2_helpers": (8, 60),
    "phase1_time_s": (1.0, 15.0),
    "phase2_time_s": (2.0, 30.0),
    "phase3_time_s": (5.0, 45.0),
}


@dataclass
class AdvisorDecision:
    params: dict[str, Any]
    rationale: str
    source: str  # "deterministic" | "claude:<model>" | "deterministic (fallback: ...)"
    usage: dict[str, Any] = field(default_factory=dict)  # tokens / cost if an LLM was used

    def to_dict(self) -> dict:
        return {"params": self.params, "rationale": self.rationale, "source": self.source, "usage": self.usage}


def clamp_params(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, (lo, hi) in BOUNDS.items():
        if k in raw and raw[k] is not None:
            try:
                v = type(lo)(raw[k])
            except (TypeError, ValueError):
                continue
            out[k] = max(lo, min(hi, v))
    if "enhanced_repair" in raw:
        out["enhanced_repair"] = bool(raw["enhanced_repair"])
    return out


class PlanningAdvisor(ABC):
    name = "abstract"

    @abstractmethod
    def suggest_repair_strategy(self, context: dict[str, Any]) -> AdvisorDecision: ...

    @abstractmethod
    def analyze_conflict(self, conflicts: list[dict[str, Any]]) -> dict[str, Any]: ...

    @abstractmethod
    def explain_plan(self, facts: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def explain_replan(self, facts: dict[str, Any], deterministic_summary: str) -> dict[str, Any]: ...
