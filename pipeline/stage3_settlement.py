#!/usr/bin/env python3
"""
Stage 3: settlement verification (NEXT BUILD — highest leverage).

Every edge we compute assumes our "observed rain" equals what settles the
market. KXRAIN settles off The Weather Company data for a specific
station/airport per city — NOT the NWS station we poll in scan.py. If those
disagree (siting, reporting lag, trace handling), DETERMINED_YES edges are
fiction.

Contract for this stage:
  In:  candidate (ticker, city, date)
  Out: pipeline_state/settlement_map.json entry:
       {city: {"settlement_station": "...", "source": "TWC",
               "nws_proxy_station": "...", "last_crosscheck": iso,
               "agreement_rate": 0.97}}
       plus per-candidate status "verified" (or "unverifiable").

Build steps:
  1. Pull each KXRAIN market's rulebook/settlement terms from the Kalshi API
     (market -> settlement / rules fields) and extract the exact station.
  2. Map every city in cities.json to its settlement station.
  3. Backtest: for the last N days, compare NWS-proxy obs vs the settlement
     feed's number; record agreement rate. Below ~0.95 agreement, the
     candidate is flagged, not traded.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PATHS, load_json  # noqa: E402


def verify_candidate(candidate_key):
    raise NotImplementedError(
        "stage3: pull KXRAIN rulebook per market, map city -> settlement "
        "station, cross-check vs NWS proxy. See PIPELINE.md.")


if __name__ == "__main__":
    print("stage3_settlement: not built yet — this is the next build.")
