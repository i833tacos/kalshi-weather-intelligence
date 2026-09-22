#!/usr/bin/env python3
"""
Stage 10: learning — score the machine against reality.

Reads the append-only ledger + settlements and answers: is the model edge
real, and where is it miscalibrated?

Metrics (per model_version, so versions compete):
  - Brier score: mean((fair - outcome)^2) over settled candidates.
  - Calibration: bucket fairs into deciles; compare mean fair vs actual
    yes-rate per bucket. Systematic over/under-confidence -> adjust blend.
  - Edge realization: predicted edge_fee at proposal vs realized pnl per
    contract. If realized << predicted, the fee/slippage model is wrong
    or the market is sharper than we think.
  - Slice by city, side (long/short), signal type, model_split flag.

Contract:
  In:  pipeline_state/ledger.jsonl + settlements.json
  Out: pipeline_state/scores.json =
       {model_version: {brier, n, calibration[{bucket, mean_fair, yes_rate}],
                        edge_realization, by_city{...}, by_signal{...}},
        recommendation: "..."}  # e.g. "raise MIN_EDGE to 0.18 on shorts"

Runs daily/weekly, or on demand. Its recommendations feed back into stage 2
thresholds and stage 4's blend weights — that's the learning loop closing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PATHS, load_json, save_json  # noqa: E402


def brier_score(pairs):
    """pairs: [(fair_prob, outcome_0/1)]. Lower is better; 0.25 = coin flip."""
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def score():
    raise NotImplementedError(
        "stage10: wire up once ledger has settled positions to score.")


if __name__ == "__main__":
    print("stage10_learning: not built yet — needs settled history first.")
