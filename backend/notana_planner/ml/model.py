"""Train, evaluate and use the re-planning strategy selector.

One gradient-boosted regressor per action predicts the outcome score of that
action from the case features; the action with the lowest predicted score is
chosen. Evaluation is k-fold cross-validation on cases the model has not seen,
reported against two references: always the default action, and the oracle
(the best action measured for each case). Nothing here can make a plan valid
or invalid: the chosen action is executed by the deterministic engine and the
result is checked by the independent validator.

The model is retrained from the case data (bundled + generated in the app) at
start-up and on request, so no binary model file has to match a library version.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .cases import ACTIONS, DEFAULT_ACTION, append_cases, load_cases
from .features import FEATURES

try:  # optional dependency: without it the system runs with the default strategy
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import KFold

    HAVE_SKLEARN = True
except Exception:  # noqa: BLE001
    HAVE_SKLEARN = False


SCORE_CAP = 2000.0  # an invalid plan (1e6) is "very bad", not 500x worse than a lost visit
MARGINS = (0.0, 25.0, 50.0, 100.0, float("inf"))  # inf = never deviate from the default


def _matrix(cases: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array([[c["features"].get(f, 0.0) for f in FEATURES] for c in cases], dtype=float)
    S = np.column_stack([[min(c["outcomes"][a]["score"], SCORE_CAP) for c in cases] for a in ACTIONS])
    return X, S


def _fit_deltas(X: np.ndarray, S: np.ndarray, seed: int = 0) -> dict[int, Any]:
    """One regressor per non-default action: predicted score minus the default's
    score on the same case (negative = expected to beat the default)."""
    di = list(ACTIONS).index(DEFAULT_ACTION)
    models = {}
    for j in range(S.shape[1]):
        if j == di:
            continue
        m = GradientBoostingRegressor(n_estimators=60, max_depth=2, learning_rate=0.05, subsample=0.8,
                                      min_samples_leaf=5, random_state=seed)
        m.fit(X, S[:, j] - S[:, di])
        models[j] = m
    return models


def _choose(models: dict[int, Any], X: np.ndarray, margin: float) -> np.ndarray:
    """Default unless some action is predicted to beat it by more than ``margin``."""
    di = list(ACTIONS).index(DEFAULT_ACTION)
    gains = np.column_stack([-models[j].predict(X) for j in sorted(models)]) if models else np.zeros((len(X), 0))
    keys = sorted(models)
    out = np.full(len(X), di)
    for i in range(len(X)):
        k = int(np.argmax(gains[i])) if gains.shape[1] else -1
        if k >= 0 and gains[i, k] > margin:
            out[i] = keys[k]
    return out


def _fit(X: np.ndarray, S: np.ndarray, seed: int = 0) -> tuple[dict[int, Any], float]:
    """Delta models + a deviation margin chosen by inner cross-validation on X/S only."""
    n = len(X)
    best_margin, best = MARGINS[-1], None
    if n >= 12:
        for margin in MARGINS:
            tot = 0.0
            for tr, te in KFold(4, shuffle=True, random_state=seed + 1).split(X):
                ch = _choose(_fit_deltas(X[tr], S[tr], seed), X[te], margin)
                tot += float(S[te, :][np.arange(len(te)), ch].sum())
            if best is None or tot < best - 1e-9:
                best, best_margin = tot, margin
    return _fit_deltas(X, S, seed), best_margin


def evaluate(cases: list[dict], folds: int = 5, seed: int = 0) -> dict:
    """Nested cross-validation: learned choice vs always-default vs oracle, every
    case scored by a model (and margin) that never saw it."""
    n = len(cases)
    if n < 2 * folds:
        return {"cases": n, "note": f"need at least {2 * folds} cases for {folds}-fold evaluation"}
    X, S = _matrix(cases)
    acts = list(ACTIONS)
    di = acts.index(DEFAULT_ACTION)
    ch = np.full(n, di)
    margins = []
    for tr, te in KFold(folds, shuffle=True, random_state=seed).split(X):
        models, margin = _fit(X[tr], S[tr], seed)
        margins.append(margin)
        ch[te] = _choose(models, X[te], margin)
    idx = np.arange(n)
    oracle = S.min(axis=1)
    learned = S[idx, ch]
    default = S[:, di]
    lost = np.array([[c["outcomes"][a]["lost"] for a in acts] for c in cases])
    return {
        "cases": n,
        "folds": folds,
        "margins_chosen": margins,
        "mean_score": {"learned": round(float(learned.mean()), 2), "default": round(float(default.mean()), 2),
                       "oracle": round(float(oracle.mean()), 2)},
        "mean_regret": {"learned": round(float((learned - oracle).mean()), 2),
                        "default": round(float((default - oracle).mean()), 2)},
        "gain_captured": round(float((default.mean() - learned.mean()) / max(1e-9, default.mean() - oracle.mean())), 3),
        "picked_best_share": {"learned": round(float(np.mean(learned <= oracle + 1e-9)), 3),
                              "default": round(float(np.mean(default <= oracle + 1e-9)), 3)},
        "better_than_default": int(np.sum(learned < default - 1e-9)),
        "worse_than_default": int(np.sum(learned > default + 1e-9)),
        "non_default_choices": int(np.sum(ch != di)),
        "lost_visits_total": {"learned": int(lost[idx, ch].sum()), "default": int(lost[:, di].sum()),
                              "oracle": int(lost[idx, S.argmin(axis=1)].sum())},
        "best_action_counts": {a: int(sum(1 for c in cases if c["best"] == a)) for a in acts},
        "chosen_action_counts": {a: int(np.sum(ch == k)) for k, a in enumerate(acts)},
    }


class StrategySelector:
    """Process-wide model holder (thread-safe retraining)."""

    def __init__(self, store: Path | None = None) -> None:
        self.store = store
        self.models: dict[int, Any] | None = None
        self.margin: float = 0.0
        self.info: dict[str, Any] = {"trained": False, "available": HAVE_SKLEARN}
        self._lock = threading.Lock()

    def cases(self) -> list[dict]:
        return load_cases(self.store)

    def train(self, evaluate_cv: bool = True) -> dict:
        if not HAVE_SKLEARN:
            self.info = {"trained": False, "available": False, "note": "scikit-learn not installed"}
            return self.info
        cases = self.cases()
        if len(cases) < 8:
            self.info = {"trained": False, "available": True, "cases": len(cases), "note": "not enough cases yet"}
            return self.info
        t0 = time.perf_counter()
        X, S = _matrix(cases)
        models, margin = _fit(X, S)
        imp = np.mean([m.feature_importances_ for m in models.values()], axis=0)
        info = {
            "trained": True,
            "available": True,
            "cases": len(cases),
            "sources": sorted({c["source"].split(",")[0] for c in cases}),
            "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "seconds": round(time.perf_counter() - t0, 2),
            "actions": list(ACTIONS),
            "default_action": DEFAULT_ACTION,
            "margin": margin,
            "method": "per-action gradient boosting on the score difference to the default; deviate only if "
                      "the predicted gain exceeds a margin chosen by inner cross-validation",
            "feature_importance": sorted(({"feature": f, "importance": round(float(v), 4)} for f, v in zip(FEATURES, imp)),
                                         key=lambda x: -x["importance"])[:10],
        }
        if evaluate_cv:
            info["evaluation"] = evaluate(cases)
        with self._lock:
            self.models, self.margin, self.info = models, margin, info
        return info

    def predict(self, features: dict[str, float]) -> dict | None:
        with self._lock:
            models, margin = self.models, self.margin
        if not models:
            return None
        x = np.array([[features.get(f, 0.0) for f in FEATURES]])
        acts = list(ACTIONS)
        gains = {acts[j]: round(float(-m.predict(x)[0]), 2) for j, m in models.items()}
        choice = acts[int(_choose(models, x, margin)[0])]
        return {"action": choice, "predicted_gain_vs_default": gains, "margin": margin, "cases": self.info.get("cases")}

    def add_cases(self, cases: list[dict]) -> None:
        if self.store is None:
            raise RuntimeError("no case store configured")
        append_cases(self.store, cases)


_SELECTOR: StrategySelector | None = None


def selector(store: Path | None = None) -> StrategySelector:
    global _SELECTOR
    if _SELECTOR is None:
        _SELECTOR = StrategySelector(store)
        _SELECTOR.train(evaluate_cv=False)
    elif store is not None and _SELECTOR.store is None:
        _SELECTOR.store = store
    return _SELECTOR


def similar_cases(features: dict[str, float], cases: list[dict], k: int = 7) -> list[dict]:
    """The k most similar past cases (standardised Euclidean distance) with their
    measured outcomes: the case base behind case-based advice."""
    if not cases:
        return []
    X = np.array([[c["features"].get(f, 0.0) for f in FEATURES] for c in cases], dtype=float)
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    x = (np.array([features.get(f, 0.0) for f in FEATURES]) - mu) / sd
    d = np.sqrt((((X - mu) / sd - x) ** 2).sum(axis=1))
    out = []
    for i in np.argsort(d)[:k]:
        c = cases[int(i)]
        out.append({
            "incident_kind": c["incident"]["kind"],
            "clock": c["incident"]["clock"],
            "released_visits": c["features"].get("released"),
            "best_action": c["best"],
            "scores": {a: o["score"] for a, o in c["outcomes"].items()},
            "lost": {a: o["lost"] for a, o in c["outcomes"].items()},
            "distance": round(float(d[i]), 2),
        })
    return out
