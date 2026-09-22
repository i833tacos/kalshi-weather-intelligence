#!/usr/bin/env python3
"""
Stage 2: candidate promotion.

Reads stage-1 scan output (latest.json) and promotes qualifying rows to
pipeline_state/candidates.json. A candidate is a contract the rest of the
pipeline is allowed to spend effort on (settlement check, L2, exec check).

Promotion rule (v1):
  - signals contain DETERMINED_YES / DETERMINED_NO, or
  - signals contain WATCH_DETERMINED_YES / WATCH_DETERMINED_NO, or
  - signals contain EDGE_LONG / EDGE_SHORT with |edge_fee| >= 0.15
Dedupe: a city+date already open as a candidate is not re-promoted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PATHS, MODEL_VERSION, now_iso, load_json, save_json,
                    record_ledger)  # noqa: E402

PROMOTE_SIGNALS = {"DETERMINED_YES", "DETERMINED_NO",
                   "WATCH_DETERMINED_YES", "WATCH_DETERMINED_NO"}
EDGE_SIGNALS = {"EDGE_LONG", "EDGE_SHORT"}
MIN_EDGE = 0.15
OPEN_STATUSES = {"new", "verified", "analyzed", "executable", "proposed"}


def promote(latest=None):
    latest = latest or load_json(PATHS["scan_latest"], {})
    cands = load_json(PATHS["candidates"], {})
    open_keys = {k for k, c in cands.items()
                 if c.get("status") in OPEN_STATUSES}
    promoted = []
    for r in latest.get("results", []):
        key = f"{r.get('city')}-{r.get('date')}"
        sigs = set(r.get("signals", []))
        edge = r.get("edge_fee")
        qualifies = bool(sigs & PROMOTE_SIGNALS)
        if (not qualifies and sigs & EDGE_SIGNALS
                and edge is not None and abs(edge) >= MIN_EDGE):
            qualifies = True
        if not qualifies or key in open_keys:
            continue
        side = "long" if (r.get("fair") or 0) >= 0.5 else "short"
        cands[key] = {
            "key": key, "city": r.get("city"), "name": r.get("name"),
            "date": r.get("date"), "ticker": r.get("ticker"),
            "fair": r.get("fair"), "edge_fee": edge,
            "breakeven": r.get("breakeven"), "fee_c": r.get("fee_c"),
            "spread": r.get("spread"), "volume": r.get("volume"),
            "obs_in": r.get("obs_in"), "ens_p": r.get("ens_p"),
            "det_agree": r.get("det_agree"),
            "model_split": r.get("model_split"),
            "basis": r.get("basis", []),
            "scan_signals": sorted(sigs),
            "side": side, "stage": "candidate", "status": "new",
            "model_version": MODEL_VERSION,
            "promoted_at": now_iso(), "expires_at": r.get("expires_at"),
        }
        record_ledger("candidate_promoted", key=key, ticker=r.get("ticker"),
                      fair=r.get("fair"), edge_fee=edge, side=side,
                      signals=sorted(sigs))
        promoted.append(key)
    save_json(PATHS["candidates"], cands)
    print(f"stage2: {len(promoted)} promoted -> {promoted}")
    return promoted


if __name__ == "__main__":
    promote()
