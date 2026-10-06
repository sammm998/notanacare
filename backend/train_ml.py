"""Generate simulated re-planning cases (in parallel) and evaluate the learned selector.

    python train_ml.py --seeds 1-24 --per-seed 6      # writes notana_planner/ml/data/replan_cases.jsonl
    python train_ml.py --evaluate                     # cross-validated evaluation only
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor

from notana_planner.ml.cases import BUNDLED, append_cases, load_cases, scenario_cases
from notana_planner.ml.model import evaluate

SIZES = [(900, 20), (1000, 22), (1200, 24), (1500, 26), (1400, 22)]


def job(args: tuple[int, int]) -> list[dict]:
    seed, n = args
    return scenario_cases(seed, n, SIZES[seed % len(SIZES)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1-12")
    ap.add_argument("--per-seed", type=int, default=6)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--evaluate", action="store_true")
    a = ap.parse_args()
    if not a.evaluate:
        lo, hi = (int(x) for x in a.seeds.split("-"))
        with ProcessPoolExecutor(a.workers) as ex:
            for cases in ex.map(job, [(s, a.per_seed) for s in range(lo, hi + 1)]):
                append_cases(BUNDLED, cases)
                print(f"+{len(cases)} cases ({cases[0]['source'] if cases else '-'})", flush=True)
    print(json.dumps(evaluate(load_cases()), indent=2))


if __name__ == "__main__":
    main()
