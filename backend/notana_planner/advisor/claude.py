"""Claude-backed PlanningAdvisor (optional; requires ANTHROPIC_API_KEY).

Claude only sees structured facts produced by the engine and returns either
bounded search parameters (validated by JSON schema, then clamped) or prose
that is explicitly marked as narrative. Any API error, refusal or schema
problem falls back to :class:`DeterministicAdvisor` -- the simulator never
depends on the LLM.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from .base import BOUNDS, AdvisorDecision, PlanningAdvisor, clamp_params
from .deterministic import DeterministicAdvisor

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output) - keep in sync with the public price list.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}

SYSTEM = (
    "You advise a deterministic home-care routing optimizer (OR-Tools + CP-SAT). "
    "You never decide whether a schedule is valid and never invent operational facts: "
    "use only the JSON facts you are given. Feasibility is decided by the optimizer and an "
    "independent validator."
)

STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "start_phase": {"type": "integer", "enum": [1, 2, 3]},
        "phase1_helpers": {"type": "integer"},
        "phase1_horizon_min": {"type": "integer"},
        "phase2_helpers": {"type": "integer"},
        "enhanced_repair": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["start_phase", "phase1_helpers", "phase1_horizon_min", "phase2_helpers", "enhanced_repair", "rationale"],
    "additionalProperties": False,
}


class ClaudePlanningAdvisor(PlanningAdvisor):
    def __init__(self, model: str | None = None, client: anthropic.Anthropic | None = None) -> None:
        self.model = model or os.environ.get("NOTANA_CLAUDE_MODEL", DEFAULT_MODEL)
        self.client = client or anthropic.Anthropic(max_retries=1, timeout=60.0)
        self.fallback = DeterministicAdvisor()
        self.name = f"claude:{self.model}"
        self.total_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}

    # ------------------------------------------------------------------
    def _call(self, prompt: str, schema: dict | None, max_tokens: int = 4000) -> tuple[str | None, dict]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": "medium"},
            # Server-side refusal fallback (routes by refusal category).
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if schema is not None:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        resp = self.client.beta.messages.create(**kwargs)
        usage = self._usage(resp)
        if resp.stop_reason == "refusal":
            return None, usage | {"refusal": True}
        text = next((b.text for b in resp.content if b.type == "text"), None)
        return text, usage

    def _usage(self, resp: Any) -> dict:
        u = getattr(resp, "usage", None)
        inp = int(getattr(u, "input_tokens", 0) or 0)
        out = int(getattr(u, "output_tokens", 0) or 0)
        pin, pout = PRICES.get(self.model, (4.0, 20.0))
        cost = inp / 1e6 * pin + out / 1e6 * pout
        self.total_usage["input_tokens"] += inp
        self.total_usage["output_tokens"] += out
        self.total_usage["cost_usd"] = round(self.total_usage["cost_usd"] + cost, 5)
        self.total_usage["calls"] += 1
        return {"input_tokens": inp, "output_tokens": out, "cost_usd": round(cost, 5), "model": self.model}

    # ------------------------------------------------------------------
    def suggest_repair_strategy(self, context: dict[str, Any]) -> AdvisorDecision:
        prompt = (
            "An operational incident hit today's home-care plan. Choose search parameters for the "
            "deterministic local-repair engine. Phase 1 = affected employees + N nearby helpers within a "
            "horizon; phase 2 = larger neighbourhood, rest of day; phase 3 = all employees. Starting too "
            "small wastes time, starting too large changes more routes (stability matters).\n"
            f"Allowed ranges: {json.dumps({k: v for k, v in BOUNDS.items()})}\n"
            f"Incident facts (JSON):\n{json.dumps(context, default=str)}"
        )
        try:
            text, usage = self._call(prompt, STRATEGY_SCHEMA)
            if text is None:
                d = self.fallback.suggest_repair_strategy(context)
                d.source = "deterministic (fallback: model refused)"
                d.usage = usage
                return d
            data = json.loads(text)
            return AdvisorDecision(clamp_params(data), str(data.get("rationale", ""))[:1000], self.name, usage)
        except (anthropic.APIError, json.JSONDecodeError, ValueError) as exc:
            d = self.fallback.suggest_repair_strategy(context)
            d.source = f"deterministic (fallback: {type(exc).__name__})"
            return d

    def analyze_conflict(self, conflicts: list[dict[str, Any]]) -> dict[str, Any]:
        base = self.fallback.analyze_conflict(conflicts)
        try:
            text, usage = self._call(
                "Summarise these unresolved home-care visits for a care manager in at most 6 short bullet "
                "points: what blocks them and the most useful manual action. Use only these facts.\n"
                + json.dumps(conflicts[:60], default=str),
                None,
            )
            if text:
                base["narrative"] = text
                base["narrative_source"] = self.name
                base["usage"] = usage
        except anthropic.APIError as exc:
            base["narrative_error"] = type(exc).__name__
        return base

    def explain_plan(self, facts: dict[str, Any]) -> dict[str, Any]:
        try:
            text, usage = self._call(
                "Explain this plan's trade-offs to a care manager in 5 sentences, using only these facts:\n"
                + json.dumps(facts, default=str),
                None,
            )
            if text:
                return {"source": self.name, "text": text, "usage": usage}
        except anthropic.APIError:
            pass
        return self.fallback.explain_plan(facts)

    def explain_replan(self, facts: dict[str, Any], deterministic_summary: str) -> dict[str, Any]:
        try:
            text, usage = self._call(
                "Rewrite this re-planning report for a care manager (clear, short, no new facts, keep every "
                "number exactly):\n" + deterministic_summary + "\nStructured facts:\n" + json.dumps(facts, default=str),
                None,
            )
            if text:
                return {"source": self.name, "text": text, "usage": usage}
        except anthropic.APIError:
            pass
        return self.fallback.explain_replan(facts, deterministic_summary)
