#!/usr/bin/env python3
"""
Stage 7: bet placement — PROPOSE-ONLY by default.

The pipeline never places a live order on its own. This module turns an
executable candidate into an order *proposal* and records it. A human
approves each proposal explicitly; only then may it be placed, and only
when live trading has been deliberately enabled.

Two hard interlocks before any order can reach Kalshi:
  1. the proposal's status is "approved" (human tap), AND
  2. pipeline_state/LIVE_TRADING_ENABLED exists (created by hand, never by
     code) containing the trader's explicit opt-in.

Auto-betting (no per-trade approval) is NOT implemented. If wanted later it
needs, set by the operator first: max stake per trade, max daily loss,
min edge, and a kill-switch.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PATHS, STATE, MODEL_VERSION, now_iso, load_json,
                    save_json, record_ledger)  # noqa: E402

LIVE_FLAG = os.path.join(STATE, "LIVE_TRADING_ENABLED")


def propose_order(candidate_key, side, size_contracts, limit_price):
    """Create an order proposal. Records it; does NOT place anything."""
    cands = load_json(PATHS["candidates"], {})
    cand = cands.get(candidate_key)
    if not cand:
        raise ValueError(f"unknown candidate {candidate_key}")
    if side not in ("yes", "no"):
        raise ValueError("side must be 'yes' or 'no'")
    pid = "prop-" + uuid.uuid4().hex[:8]
    # Kalshi taker fee estimate at the limit price (cents/contract)
    p = max(0.01, min(0.99, limit_price))
    import math
    fee_c = math.ceil(7 * p * (1 - p) - 1e-9)
    notional_c = int(round(limit_price * 100)) * size_contracts
    proposals = load_json(PATHS["proposals"], {})
    proposals[pid] = {
        "id": pid, "candidate_key": candidate_key,
        "ticker": cand.get("ticker"), "side": side,
        "size_contracts": size_contracts, "limit_price": limit_price,
        "est_fee_c_per_contract": fee_c,
        "est_total_cost_c": notional_c + fee_c * size_contracts,
        "max_loss_c": notional_c + fee_c * size_contracts,  # buy yes/no, lose all
        "fair": cand.get("fair"), "edge_fee": cand.get("edge_fee"),
        "model_version": MODEL_VERSION,
        "created_at": now_iso(), "status": "awaiting_approval",
    }
    save_json(PATHS["proposals"], proposals)
    cand["status"] = "proposed"
    cands[candidate_key] = cand
    save_json(PATHS["candidates"], cands)
    record_ledger("proposal_created", proposal_id=pid,
                  candidate_key=candidate_key, ticker=cand.get("ticker"),
                  side=side, size_contracts=size_contracts,
                  limit_price=limit_price, est_fee_c=fee_c)
    print(f"stage7: proposal {pid} awaiting approval "
          f"({side} {size_contracts}x {cand.get('ticker')} @ {limit_price:.2f})")
    return pid


def approve_proposal(pid, by="operator"):
    """Human approval. Marks approved; still does NOT place the order."""
    proposals = load_json(PATHS["proposals"], {})
    prop = proposals.get(pid)
    if not prop:
        raise ValueError(f"unknown proposal {pid}")
    if prop["status"] != "awaiting_approval":
        raise ValueError(f"proposal {pid} is {prop['status']}, cannot approve")
    prop["status"] = "approved"
    prop["approved_by"] = by
    prop["approved_at"] = now_iso()
    save_json(PATHS["proposals"], proposals)
    record_ledger("proposal_approved", proposal_id=pid, by=by)
    print(f"stage7: proposal {pid} approved by {by} (not yet placed)")
    return prop


def place_order(pid):
    """Place an approved proposal. Refuses unless BOTH interlocks hold."""
    proposals = load_json(PATHS["proposals"], {})
    prop = proposals.get(pid)
    if not prop:
        raise ValueError(f"unknown proposal {pid}")
    if prop.get("status") != "approved":
        raise RuntimeError(
            f"REFUSED: proposal {pid} is '{prop.get('status')}', not 'approved'. "
            "Human approval is required before any order.")
    if not os.path.exists(LIVE_FLAG):
        raise RuntimeError(
            "REFUSED: live trading is not enabled. Create "
            "pipeline_state/LIVE_TRADING_ENABLED by hand with your explicit "
            "opt-in before any order can be placed. The pipeline will never "
            "create this file itself.")
    # Live order execution is not wired yet: needs Kalshi API credentials
    # and the funded-account decision. This is the exact insertion point.
    raise NotImplementedError(
        "Live order execution not wired yet: needs Kalshi API key + funded "
        f"account. Proposal {pid} is approved and ready; wire placement here.")


if __name__ == "__main__":
    print("stage7_bet: propose-only gate. Import and call propose_order(),")
    print("then approve_proposal(), then place_order() — the last refuses")
    print("until both interlocks (human approval + LIVE_TRADING_ENABLED) hold.")
