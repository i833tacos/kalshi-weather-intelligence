#!/usr/bin/env python3
"""
Weather-brain scoring: fast analysis that says where to invest.

  log_scan()        - append this scan's per-city signals+fairs to history
  refresh_candidates() - re-sync candidates vs the latest scan; invalidate
                      when the fair moved materially against the thesis
                      (e.g. MIA 2026-09-21: 0.06" obs revised to 0.00")
  verify_date()     - check past signals against actual outcomes (NWS obs)
  score_model()     - hit rate on determined calls, Brier on fairs,
                      per-city reliability -> scores.json
  rank_invest()     - ranked invest list over open candidates

Run: python3 pipeline/scoring.py [log|refresh|verify|score|rank|all]
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (PATHS, STATE, MODEL_VERSION, now_iso, load_json,  # noqa: E402
                    save_json, record_ledger)
from scan import nws_observations, load_cities  # noqa: E402

HISTORY = os.path.join(STATE, "signal_history.jsonl")
VERIF = os.path.join(STATE, "verifications.json")
INVALIDATE_FAIR_MOVE = 0.30   # fair moved this much vs candidate -> invalidate


# ---------- stage 1b: history ----------
def log_scan(latest=None):
    latest = latest or load_json(PATHS["scan_latest"], {})
    scanned_at = latest.get("scanned_at", now_iso())
    os.makedirs(STATE, exist_ok=True)
    n = 0
    with open(HISTORY, "a") as f:
        for r in latest.get("results", []):
            f.write(__import__("json").dumps({
                "scanned_at": scanned_at,
                "city": r.get("city"), "date": r.get("date"),
                "fair": r.get("fair"), "edge_fee": r.get("edge_fee"),
                "bid": r.get("bid"), "ask": r.get("ask"),
                "obs_in": r.get("obs_in"), "signals": r.get("signals", []),
                "model_version": MODEL_VERSION,
            }) + "\n")
            n += 1
    print(f"scoring: logged {n} rows to signal_history.jsonl")
    return n


# ---------- candidate refresh / invalidation ----------
def refresh_candidates(latest=None):
    """Sync open candidates with the newest scan. Invalidate on thesis break."""
    latest = latest or load_json(PATHS["scan_latest"], {})
    cands = load_json(PATHS["candidates"], {})
    by_key = {f"{r.get('city')}-{r.get('date')}": r
              for r in latest.get("results", [])}
    changed, invalidated = [], []
    for key, c in cands.items():
        if c.get("status") not in ("new", "verified", "analyzed",
                                   "executable", "proposed"):
            continue
        r = by_key.get(key)
        if r is None:
            continue
        old_fair, new_fair = c.get("fair"), r.get("fair")
        c["fair"] = new_fair
        c["edge_fee"] = r.get("edge_fee")
        c["breakeven"] = r.get("breakeven")
        c["spread"] = r.get("spread")
        c["volume"] = r.get("volume")
        c["obs_in"] = r.get("obs_in")
        c["scan_signals"] = r.get("signals", [])
        c["refreshed_at"] = now_iso()
        if (old_fair is not None and new_fair is not None
                and abs(new_fair - old_fair) >= INVALIDATE_FAIR_MOVE):
            c["status"] = "invalidated"
            c["invalidate_reason"] = (
                f"fair moved {old_fair:.2f} -> {new_fair:.2f}")
            invalidated.append(key)
            record_ledger("candidate_invalidated", key=key,
                          old_fair=old_fair, new_fair=new_fair,
                          reason=c["invalidate_reason"])
        else:
            changed.append(key)
    save_json(PATHS["candidates"], cands)
    print(f"scoring: refreshed {len(changed)}, invalidated {invalidated}")
    return changed, invalidated


# ---------- verification ----------
def actual_rain_yes(city, datestr):
    """Did >= 0.01" fall that day per the NWS station? None if unknowable."""
    tz = ZoneInfo(city["tz"])
    y, m, d = int(datestr[0:2]), None, None
    # datestr like 26SEP21
    import calendar
    mon = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    yy = 2000 + int(datestr[0:2])
    mm = mon[datestr[2:5]]
    dd = int(datestr[5:7])
    day_start = datetime(yy, mm, dd, tzinfo=tz).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    now_utc = datetime.now(timezone.utc)
    if now_utc < day_end:
        return None  # day not over yet
    obs_in, _ = nws_observations(city["station"], day_start,
                                 min(now_utc, day_end))
    if obs_in is None:
        return None
    return obs_in >= 0.01


def verify_date(datestr, cities=None):
    """Verify every determined/forecast signal for a past date."""
    cities = cities or load_cities()
    by_code = {c["code"]: c for c in cities}
    verif = load_json(VERIF, {})
    rows = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {}
        for c in cities:
            futs[ex.submit(actual_rain_yes, c, datestr)] = c["code"]
        outcomes = {code: f.result() for f, code in futs.items()}
    # pull the last logged fair+signals per city for that date
    last = {}
    try:
        with open(HISTORY) as f:
            import json as js
            for line in f:
                try:
                    e = js.loads(line)
                except Exception:
                    continue
                if e.get("date") == datestr:
                    last[e["city"]] = e
    except FileNotFoundError:
        pass
    for code, rained in outcomes.items():
        e = last.get(code, {})
        rows.append({
            "city": code, "date": datestr,
            "rained": rained,
            "fair": e.get("fair"), "signals": e.get("signals", []),
            "model_version": e.get("model_version"),
        })
    verif[datestr] = {"verified_at": now_iso(), "rows": rows}
    save_json(VERIF, verif)
    n = sum(1 for r in rows if r["rained"] is not None)
    print(f"scoring: verified {n}/{len(rows)} cities for {datestr}")
    return rows


# ---------- model scorecard ----------
def score_model():
    """Hit rate on determined calls + Brier on fairs + per-city reliability."""
    verif = load_json(VERIF, {})
    det_hits, det_n = 0, 0
    brier_pairs = []
    city_stats = {}
    for datestr, v in verif.items():
        for r in v.get("rows", []):
            rained = r["rained"]
            if rained is None:
                continue
            outcome = 1 if rained else 0
            sigs = set(r.get("signals", []))
            fair = r.get("fair")
            st = city_stats.setdefault(r["city"],
                                       {"hits": 0, "n": 0, "brier": []})
            if "WATCH_DETERMINED_YES" in sigs or "DETERMINED_YES" in sigs:
                det_n += 1
                det_hits += outcome
                st["n"] += 1
                st["hits"] += outcome
            elif "WATCH_DETERMINED_NO" in sigs or "DETERMINED_NO" in sigs:
                det_n += 1
                det_hits += (1 - outcome)
                st["n"] += 1
                st["hits"] += (1 - outcome)
            if fair is not None:
                brier_pairs.append((fair, outcome))
                st["brier"].append((fair - outcome) ** 2)
    reliability = {}
    for city, st in city_stats.items():
        reliability[city] = {
            "hit_rate": round(st["hits"] / st["n"], 3) if st["n"] else None,
            "n": st["n"],
            "brier": round(sum(st["brier"]) / len(st["brier"]), 4)
            if st["brier"] else None,
        }
    scores = {
        "scored_at": now_iso(),
        "determined_hit_rate": round(det_hits / det_n, 3) if det_n else None,
        "determined_n": det_n,
        "brier": round(sum((p - o) ** 2 for p, o in brier_pairs)
                       / len(brier_pairs), 4) if brier_pairs else None,
        "brier_n": len(brier_pairs),
        "city_reliability": reliability,
    }
    save_json(PATHS["scores"], scores)
    print(f"scoring: determined {det_hits}/{det_n} "
          f"({scores['determined_hit_rate']}), "
          f"brier {scores['brier']} (n={scores['brier_n']})")
    return scores


# ---------- invest ranking ----------
def rank_invest(top=10):
    """Ranked invest list over open candidates. Fast, explainable."""
    cands = load_json(PATHS["candidates"], {})
    scores = load_json(PATHS["scores"], {})
    rel = scores.get("city_reliability", {})
    ranked = []
    for key, c in cands.items():
        if c.get("status") not in ("new", "verified", "analyzed",
                                   "executable", "proposed"):
            continue
        fair = c.get("fair")
        if fair is None:
            continue
        conviction = abs(fair - 0.5) * 2                      # 0..1
        edge = c.get("edge_fee")
        has_book = c.get("spread") is not None
        edge_c = (min(abs(edge), 0.5) * 2 if edge is not None and has_book
                  else 0.3)
        conf = 1.0
        if c.get("model_split"):
            conf *= 0.7
        det_a, ens_p = c.get("det_agree"), c.get("ens_p")
        # ens_p isn't stored on candidates; pull from latest scan instead
        if det_a is not None and ens_p is not None:
            conf *= 0.7 + 0.3 * (1 - abs(det_a - ens_p))
        city_rel = (rel.get(c["city"], {}) or {}).get("hit_rate")
        city_rel = city_rel if city_rel is not None else 0.5
        liquidity = 1.0 if has_book else 0.35
        score = (100 * conviction * conf * (0.5 + 0.5 * edge_c)
                 * (0.6 + 0.4 * city_rel) * liquidity)
        why = []
        why.append(f"fair {fair:.2f}")
        if edge is not None and has_book:
            why.append(f"edge {edge:+.2f}")
        else:
            why.append("no book yet — watch")
        if c.get("model_split"):
            why.append("model split: lower confidence")
        obs_in = c.get("obs_in") or 0
        if obs_in:
            why.append(f"{obs_in:.2f}in observed")
        # MIA lesson: a determined-yes resting just above the 0.01" line
        # can flip on an observation revision.
        if fair and fair > 0.9 and 0 < obs_in < 0.03:
            why.append("thin margin — revision risk")
            score *= 0.85
        ranked.append({
            "key": key, "side": c.get("side"), "ticker": c.get("ticker"),
            "fair": fair, "edge_fee": edge, "has_book": has_book,
            "score": round(score, 1), "why": "; ".join(why),
        })
    ranked.sort(key=lambda r: -r["score"])
    print(f"{'rank':4s} {'candidate':13s} {'side':5s} {'fair':>5s} "
          f"{'edge':>6s} {'book':>4s} {'score':>6s}  why")
    for i, r in enumerate(ranked[:top], 1):
        edge = f"{r['edge_fee']:+.2f}" if r["edge_fee"] is not None else "  --  "
        print(f"{i:<4d} {r['key']:13s} {r['side']:5s} {r['fair']:>5.2f} "
              f"{edge:>6s} {'yes' if r['has_book'] else 'no':>4s} "
              f"{r['score']:>6.1f}  {r['why']}")
    return ranked


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "all"
    if cmd in ("log", "all"):
        log_scan()
    if cmd in ("refresh", "all"):
        refresh_candidates()
    if cmd in ("verify", "all"):
        today = datetime.now(ZoneInfo("America/Denver")).date()
        # verify dates with history that are fully in the past
        verify_date((today - timedelta(days=1)).strftime("%y%b%d").upper())
    if cmd in ("score", "all"):
        score_model()
    if cmd in ("rank", "all"):
        rank_invest()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
