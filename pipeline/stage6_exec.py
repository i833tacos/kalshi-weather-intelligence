#!/usr/bin/env python3
"""
Stage 6: executable-price / fee check.

A top-of-book quote is not a fill. This stage answers: "if I send size N
right now, what do I actually pay all-in?"

Contract:
  In:  candidate (with fair, side)
  Out: pipeline_state/exec_checks.json[{candidate_key}] =
       {size_contracts, quoted_bid/ask, est_fill_price, fee_c_total,
        slippage_c, executable_edge_fee, passes (bool), at}

Method:
  - Pull the full orderbook (Kalshi /markets/{ticker}/orderbook), not just
    top bid/ask; walk the ladder for the requested size.
  - est_fill_price = size-weighted average + half-spread slippage buffer.
  - fee from Kalshi taker schedule on the fill price.
  - executable_edge_fee = fair - (fill + fee) for longs.
  - passes = executable_edge_fee >= MIN_EXEC_EDGE (default 0.10) AND
    book depth covers size without crossing > MAX_SLIPPAGE.

hotwatch.py's book-appearance polling migrates here long-term.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402

MIN_EXEC_EDGE = 0.10


def exec_check(candidate_key, size_contracts=10):
    raise NotImplementedError(
        "stage6: walk the Kalshi orderbook ladder for size, compute "
        "all-in fill + fee + slippage -> executable edge.")


if __name__ == "__main__":
    print("stage6_exec: not built yet (hotwatch.py covers book-appearance).")
