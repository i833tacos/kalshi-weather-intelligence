#!/usr/bin/env python3
"""
Kalshi rain hot-watch.

Polls every contract on the scan.py hotlist (strong model lean, no usable book
at scan time) and prints an ALERT line the moment a book appears with
fee-adjusted edge >= HOT_EDGE.

State (hotwatch_state.json) tracks per-contract alert/dark counters so a book
that appears, vanishes, and reappears re-alerts, but a resting book never
double-alerts. Read-only: never places orders.
"""
import json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import market_quote, taker_fee_c  # noqa: E402

GOAL_DIR = os.path.expanduser("~/workspace/goals/kalshi-rain-watch/hidden_files")
HOTLIST_PATH = os.path.join(GOAL_DIR, "hotlist.json")
STATE_PATH = os.path.join(GOAL_DIR, "hotwatch_state.json")
TEMP_HOTLIST_PATH = os.path.join(GOAL_DIR, "temp_hotlist.json")
TEMP_STATE_PATH = os.path.join(GOAL_DIR, "temp_hotwatch_state.json")

HOT_EDGE = 0.15      # alert when fee-adjusted edge >= 15c
MAX_DARK = 3         # polls with no book before a contract re-arms


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def watch(hotlist_path, state_path, tag):
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    hotlist = [e for e in load_json(hotlist_path, [])
               if e.get("expires_at", "") > now_iso and e.get("ticker")]
    state = load_json(state_path, {})
    alerts = []
    for e in hotlist:
        key = e["key"]
        st = state.setdefault(key, {"alerted": False, "dark": 0})
        q = market_quote(e["ticker"])
        if q and q.get("status") not in (None, "active"):
            st["dead"] = True
            continue
        book = bool(q and q["bid"] is not None and q["ask"] is not None)
        if book:
            st["dark"] = 0
            fair, side = e["fair"], e["side"]
            if side == "long":
                fee = taker_fee_c(q["ask"]) / 100.0
                price, breakeven, edge = q["ask"], q["ask"] + fee, fair - (q["ask"] + fee)
            else:
                no_p = 1 - q["bid"]
                fee = taker_fee_c(no_p) / 100.0
                price, breakeven = q["bid"], q["bid"] - fee
                edge = (q["bid"] - fee) - fair
            if not st["alerted"] and edge >= HOT_EDGE:
                st["alerted"] = True
                st["alerted_at"] = now_iso
                alert = {
                    "key": key, "kind": e.get("kind", "rain"),
                    "city": e["city"], "name": e.get("name"),
                    "date": e["date"], "side": side,
                    "label": e.get("label"),
                    "bid": round(q["bid"], 3), "ask": round(q["ask"], 3),
                    "price": round(price, 3),
                    "fair": fair, "breakeven": round(breakeven, 4),
                    "edge_fee": round(edge, 4), "fee_c": round(fee * 100),
                    "at": now_iso,
                }
                alerts.append(alert)
                print("ALERT " + json.dumps(alert))
        else:
            st["dark"] = st.get("dark", 0) + 1
            if st["dark"] >= MAX_DARK:
                st["alerted"] = False
                st.pop("alerted_at", None)
                st["dark"] = 0
    # prune state for contracts no longer watched / dead
    live_keys = {e["key"] for e in hotlist}
    state = {k: v for k, v in state.items()
             if k in live_keys and not v.get("dead")}
    os.makedirs(GOAL_DIR, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump({"updated": now_iso, "contracts": state}, f, indent=1)
    print(f"hotwatch[{tag}]: {len(hotlist)} watched, {len(alerts)} alerts")
    return alerts


def main():
    alerts = []
    alerts += watch(HOTLIST_PATH, STATE_PATH, "rain")
    alerts += watch(TEMP_HOTLIST_PATH, TEMP_STATE_PATH, "temp")
    return alerts


if __name__ == "__main__":
    main()
