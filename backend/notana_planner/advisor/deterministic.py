"""Deterministic fallback advisor (always available, no network, no cost)."""

from __future__ import annotations

from typing import Any

from .base import AdvisorDecision, PlanningAdvisor, clamp_params


class DeterministicAdvisor(PlanningAdvisor):
    name = "deterministic"

    def suggest_repair_strategy(self, context: dict[str, Any]) -> AdvisorDecision:
        sim = context.get("similar_cases") or []
        if len(sim) >= 3:
            # Case-based reasoning: the action with the best mean measured score on the
            # most similar past cases (ties -> the default local escalation).
            acts = list(sim[0]["scores"])
            mean = {a: sum(c["scores"][a] for c in sim) / len(sim) for a in acts}
            best = min(acts, key=lambda a: (round(mean[a], 6), a != "local"))
            p = {"local": {}, "local_enhanced": {"enhanced_repair": True}, "expanded": {"start_phase": 2},
                 "broad": {"start_phase": 3}}.get(best, {})
            why = (f"case-based: on the {len(sim)} most similar past cases '{best}' had the best mean score "
                   + ", ".join(f"{a} {mean[a]:.0f}" for a in acts))
            return AdvisorDecision(clamp_params(dict(p)), why, "deterministic (case-based)")
        kind = context.get("incident_kind")
        released = int(context.get("released_visits", 0))
        affected = int(context.get("affected_employees", 0))
        high = int(context.get("released_high_priority", 0))
        util = float(context.get("utilization", 0.6))
        p: dict[str, Any] = {}
        why = []
        if kind == "traffic" and affected > 25:
            p["start_phase"] = 2
            why.append(f"traffic affects {affected} routes: a small neighbourhood cannot absorb it, start at phase 2")
        elif released > 12 or affected > 6:
            p["start_phase"] = 1
            p["phase1_helpers"] = 12
            p["phase1_horizon_min"] = 240
            why.append(f"{released} released visits: widen the local neighbourhood (12 helpers, 4h horizon)")
        else:
            why.append("small incident: default local repair first")
        if high:
            p["enhanced_repair"] = True
            why.append(f"{high} high-priority visits released: enable ejection-chain repair")
        if util > 0.62:
            p["phase2_helpers"] = 36
            why.append(f"high utilisation ({util:.0%}): larger phase-2 neighbourhood")
        return AdvisorDecision(clamp_params(p), "; ".join(why), "deterministic")

    def analyze_conflict(self, conflicts: list[dict[str, Any]]) -> dict[str, Any]:
        by_code: dict[str, list[str]] = {}
        for c in conflicts:
            by_code.setdefault(c.get("code", "UNKNOWN"), []).append(c.get("visit_id", "?"))
        actions = {
            "INSUFFICIENT_STAFFING": "add capacity in the window (pool staff, extend a shift, approve overtime)",
            "TRAVEL_FEASIBILITY": "assign a closer employee or widen the time window",
            "NO_EMPLOYEE_WITH_DELEGATION": "arrange a delegation or a nurse visit",
            "QUALIFIED_STAFF_UNAVAILABLE": "call in a qualified replacement",
            "EMPLOYEE_SHIFT_ENDED": "extend a qualified employee's shift or move the visit",
            "DOUBLE_STAFFING_UNAVAILABLE": "pair two employees for that slot or move the visit",
            "CONFLICTING_HIGH_PRIORITY": "accept the trade-off or add capacity",
            "SOLVER_SEARCH_LIMIT": "re-run with a longer time limit",
            "IMPOSSIBLE_TIME_WINDOW": "correct conflicting intervention times",
        }
        items = [
            {"code": k, "count": len(v), "visits": v[:10], "suggested_action": actions.get(k, "manual review")}
            for k, v in sorted(by_code.items(), key=lambda x: -len(x[1]))
        ]
        return {"source": "deterministic", "groups": items}

    def explain_plan(self, facts: dict[str, Any]) -> dict[str, Any]:
        return {"source": "deterministic", "text": facts.get("text", "")}

    def explain_replan(self, facts: dict[str, Any], deterministic_summary: str) -> dict[str, Any]:
        return {"source": "deterministic", "text": deterministic_summary}
