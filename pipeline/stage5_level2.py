#!/usr/bin/env python3
"""
Stage 5: Level 2 analysis (optional, per-candidate deep dive).

Only runs on candidates — never on the full 27-city scan — so it can afford
expensive inputs. Produces a refined fair + confidence.

Planned inputs:
  - HRRR (3km, hourly, short-range) for rest-of-day precip when < 12h to expiry
  - Radar trend: is precip currently falling within ~50km of the settlement
    station and moving toward/away?
  - Model spread: ensemble IQR / fraction of members near the 0.01" line —
    tight spread near the line = low confidence even if mean looks clear
  - NWS mesoscale discussions / forecast discussion headlines

Contract:
  In:  candidate (+ stage-4 base forecast)
  Out: pipeline_state/level2.json[{candidate_key}] =
       {fair_refined, confidence (0..1), drivers[], at}
  A candidate with confidence < threshold stays a candidate; it does not
  advance to exec check.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402


def level2_analyze(candidate_key):
    raise NotImplementedError(
        "stage5: HRRR + radar trend + ensemble spread, candidates only.")


if __name__ == "__main__":
    print("stage5_level2: not built yet.")
