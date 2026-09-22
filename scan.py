#!/usr/bin/env python3
"""
Kalshi KXRAIN rain-market anomaly scanner v2.

Pipeline per city per event day:
  Kalshi market (bid/ask/last/volume)
    vs
  NWS station observations (rain already fallen today)
  NWS gridpoint forecast (QPF + PoP for the rest of the day)
  Open-Meteo GFS 31-member ensemble -> P(any rain before local midnight)
  Open-Meteo deterministic consensus -> GFS + ECMWF IFS + ICON agreement

Fair value = mean of ensemble P(rain) and deterministic-model agreement
(unless rain already observed -> 0.99 determined, or dry all day -> 0.01).

Edges are FEE-ADJUSTED using Kalshi's taker formula:
    fee_c = ceil(7 * p * (1 - p))  cents per contract, p in 0..1
so a "12c edge" means 12c after the taker fee, not before.

Signals:
  DETERMINED_YES / DETERMINED_NO  - outcome effectively known, market hasn't caught up
  EDGE_LONG / EDGE_SHORT          - fee-adjusted model edge vs the book
  MODEL_SPLIT                   - informational: a deterministic model is bone-dry
                                  while the rest of the model suite is wet
                                  (e.g. GFS 0mm vs 31/31 ensemble members wet)
  NO_LIQUIDITY                   - market listed but no book (hotlist watches these)
  NO_DATA                        - weather feed failed for this city

Contracts with |fair-lean| >= HOTLIST_LEAN and no usable book are written to
hotlist.json for hotwatch.py (2-5 min book-appearance polling).

State lives in ../goals/kalshi-rain-watch/hidden_files/rain_state.json so
scheduled runs only alert on NEW or CHANGED signals.
"""
import json, math, os, sys, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UA = {"User-Agent": "kilo-rain-scanner/2.0"}
HERE = os.path.dirname(os.path.abspath(__file__))
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GOAL_DIR = os.path.expanduser("~/workspace/goals/kalshi-rain-watch/hidden_files")
STATE_PATH = os.path.join(GOAL_DIR, "rain_state.json")
LATEST_PATH = os.path.join(HERE, "latest.json")
HOTLIST_PATH = os.path.join(GOAL_DIR, "hotlist.json")

MM_PER_IN = 25.4
WET_MEMBER_MM = 0.2          # ensemble member counts as wet if any hour > this (mm)
WET_MODEL_MM = MM_PER_IN * 0.01  # deterministic model counts as wet at >= 0.01"
DET_MODELS = ["gfs_seamless", "ecmwf_ifs", "icon_seamless"]
EDGE_THRESHOLD = 0.12        # flag fee-adjusted edge >= 12c
MAX_SPREAD = 0.25            # ignore edges on books wider than 25c
HOTLIST_LEAN = 0.15          # |fair - 0.5| >= 15c earns a hotlist slot (fair>=.65 or <=.35)


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def taker_fee_c(price):
    """Kalshi taker fee in cents for one contract at `price` (0..1 dollars).

    fee = round_up(0.07 * contracts * price * (1 - price)) per Kalshi's schedule.
    """
    if price is None or not (0 < price < 1):
        return 0
    return math.ceil(7 * price * (1 - price) - 1e-9)


def load_cities():
    with open(os.path.join(HERE, "cities.json")) as f:
        return json.load(f)["cities"]


def event_datestr(dt):
    return dt.strftime("%y%b%d").upper()  # 26SEP21


# ---------------- Kalshi ----------------
def kalshi_markets(datestr):
    try:
        d = get(f"{KALSHI}/events/KXRAIN-{datestr}?with_nested_markets=true")
        return {m["ticker"].rsplit("-", 1)[-1]: m
                for m in d.get("event", {}).get("markets", [])}
    except Exception:
        return {}


def market_quote(ticker):
    try:
        m = get(f"{KALSHI}/markets/{ticker}").get("market", {})
        bid, ask = m.get("yes_bid"), m.get("yes_ask")
        return {
            "bid": bid / 100.0 if bid is not None else None,
            "ask": ask / 100.0 if ask is not None else None,
            "spread": (ask - bid) / 100.0 if bid is not None and ask is not None else None,
            "volume": m.get("volume"),
            "open_interest": m.get("open_interest"),
            "status": m.get("status"),
        }
    except Exception:
        return None


# ---------------- NWS ----------------
def nws_observations(station, day_start_utc, now_utc):
    """Total observed precip (inches) since local midnight, + data coverage.

    NOTE: stations report hourly OR every ~5 min. limit=400 covers a full day
    at any cadence. Obs are deduplicated to one per clock hour (latest obs in
    the hour) so 5-min stations don't 12x-count precipitationLastHour.
    """
    try:
        d = get(f"https://api.weather.gov/stations/{station}/observations?limit=400")
    except Exception:
        return None, 0.0
    by_hour = {}
    for f in d.get("features", []):
        p = f["properties"]
        try:
            ts = datetime.fromisoformat(p["timestamp"])
        except Exception:
            continue
        if ts < day_start_utc or ts > now_utc:
            continue
        # bucket by UTC clock hour; keep the latest obs in each hour
        bucket = ts.replace(minute=0, second=0, microsecond=0)
        if bucket not in by_hour or ts > by_hour[bucket][0]:
            by_hour[bucket] = (ts, (p.get("precipitationLastHour") or {}).get("value"))
    total_mm, hours_seen = 0.0, 0
    for _ts, v in by_hour.values():
        if v is not None:
            total_mm += v
            hours_seen += 1
    hours_possible = len(by_hour)
    coverage = hours_seen / hours_possible if hours_possible else 0.0
    return total_mm / MM_PER_IN, coverage


def nws_gridpoint(lat, lon, day_start_utc, day_end_utc):
    """(QPF mm remaining today, max PoP remaining today) from NWS gridpoint forecast."""
    try:
        pt = get(f"https://api.weather.gov/points/{lat},{lon}")["properties"]
        gd = get(pt["forecastGridData"])["properties"]
    except Exception:
        return None, None
    qpf_rem, pop_max = 0.0, 0
    for v in gd.get("quantitativePrecipitation", {}).get("values", []):
        try:
            start_s, dur_s = v["validTime"].split("/")
            start = datetime.fromisoformat(start_s)
        except Exception:
            continue
        hours = 6
        try:
            num = "".join(c for c in dur_s if c.isdigit() or c == ".")
            hours = float(num) * (1 / 60 if "M" in dur_s and "H" not in dur_s else 1)
        except Exception:
            pass
        end = start + timedelta(hours=hours)
        if end > day_start_utc and start < day_end_utc and (v.get("value") or 0) > 0:
            overlap_start = max(start, datetime.now(timezone.utc))
            frac = max(0.0, (end - overlap_start).total_seconds() / (end - start).total_seconds())
            qpf_rem += v["value"] * frac
    for v in gd.get("probabilityOfPrecipitation", {}).get("values", []):
        try:
            start = datetime.fromisoformat(v["validTime"].split("/")[0])
        except Exception:
            continue
        if start >= day_start_utc and start < day_end_utc and start >= datetime.now(timezone.utc) - timedelta(hours=3):
            if v.get("value") is not None:
                pop_max = max(pop_max, v["value"])
    return qpf_rem, pop_max


# ---------------- Open-Meteo ----------------
def _window_idx(times, start_utc, end_utc):
    idx = []
    for i, t in enumerate(times):
        try:
            ts = datetime.fromisoformat(t)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if start_utc <= ts < end_utc:
            idx.append(i)
    return idx


def ensemble_p_rain(lat, lon, now_utc, day_end_utc):
    """Fraction of GFS ensemble members with >WET_MEMBER_MM precip before local midnight."""
    try:
        d = get("https://ensemble-api.open-meteo.com/v1/ensemble?latitude=%.3f&longitude=%.3f"
                "&hourly=precipitation&models=gfs_seamless&forecast_days=2&timezone=UTC" % (lat, lon))
    except Exception:
        return None
    h = d.get("hourly", {})
    idx = _window_idx(h.get("time", []), now_utc, day_end_utc)
    if not idx:
        return None
    members = [k for k in h if k.startswith("precipitation")]
    if not members:
        return None
    wet = sum(1 for k in members if any((h[k][i] or 0) > WET_MEMBER_MM for i in idx))
    return wet / len(members)


def det_model_rain(lat, lon, now_utc, day_end_utc):
    """Total precip (mm) before local midnight for each deterministic model.
    One call, per-model keys: precipitation_gfs_seamless etc."""
    try:
        d = get("https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f"
                "&hourly=precipitation&models=%s&forecast_days=2&timezone=UTC"
                % (lat, lon, ",".join(DET_MODELS)))
    except Exception:
        return None
    h = d.get("hourly", {})
    idx = _window_idx(h.get("time", []), now_utc, day_end_utc)
    if not idx:
        return None
    out = {}
    for mname in DET_MODELS:
        vals = h.get(f"precipitation_{mname}")
        if vals and len(vals) > max(idx):
            out[mname] = round(sum((vals[i] or 0) for i in idx), 2)
    return out or None


# ---------------- scan ----------------
def scan_city(city, markets, datestr, is_today):
    tz = ZoneInfo(city["tz"])
    now_local = datetime.now(tz)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if not is_today:
        day_start_local += timedelta(days=1)
    day_start_utc = day_start_local.astimezone(timezone.utc)
    day_end_utc = (day_start_local + timedelta(days=1)).astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    win_start = max(now_utc, day_start_utc)

    m = markets.get(city["code"])
    ticker = m["ticker"] if m else None
    quote = market_quote(ticker) if ticker else None

    obs_in, coverage = (0.0, 1.0) if not is_today else nws_observations(city["station"], day_start_utc, now_utc)
    qpf_mm, pop = nws_gridpoint(city["lat"], city["lon"], day_start_utc, day_end_utc)
    p_ens = ensemble_p_rain(city["lat"], city["lon"], win_start, day_end_utc)
    det_mm = det_model_rain(city["lat"], city["lon"], win_start, day_end_utc)

    det_agree, model_split, dry_models = None, False, []
    if det_mm:
        votes = [mm >= WET_MODEL_MM for mm in det_mm.values()]
        det_agree = round(sum(votes) / len(votes), 3)
        dry_models = [k.replace("_seamless", "").replace("gfs", "GFS").replace("ecmwf_ifs", "ECMWF").replace("icon", "ICON")
                      for k, mm in det_mm.items() if mm == 0]
        wet_consensus = (p_ens or 0) >= 0.5 or det_agree >= 0.5
        if dry_models and wet_consensus:
            model_split = True

    # ---- fair value ----
    fair, basis, determined = None, [], False
    if obs_in is None:
        fair = None
    elif obs_in >= 0.01:
        # NOTE: contract settles > 0" measurable; trace counts as zero.
        # NWS reports trace as 0, so obs >= 0.01" is genuinely measurable.
        fair, determined = 0.99, True
        basis.append(f"{obs_in:.2f}in already observed")
    else:
        probs = [p for p in (p_ens, det_agree) if p is not None]
        if probs:
            fair = round(sum(probs) / len(probs), 3)
            parts = []
            if p_ens is not None:
                parts.append(f"GFS-ens {p_ens:.0%}")
            if det_agree is not None:
                parts.append(f"det {det_agree:.0%}")
            basis.append("model blend: " + " + ".join(parts))
        elif pop:
            fair = round(pop / 100.0, 3)
            basis.append(f"NWS PoP {pop}% (fallback)")
        local_hour = now_local.hour if is_today else 0
        if (fair is not None and is_today and local_hour >= 12 and fair <= 0.03
                and (qpf_mm or 0) == 0 and coverage >= 0.5):
            fair, determined = 0.01, True
            basis.append("no rain so far, models dry rest of day")
    if det_mm:
        basis.append("rest-of-day mm: " + " / ".join(
            f"{k.replace('_seamless', '').replace('ecmwf_ifs', 'ECMWF').upper()} {v}"
            for k, v in det_mm.items()))
    if model_split:
        basis.append(f"MODEL SPLIT: {', '.join(dry_models)} dry vs wet consensus")

    # ---- fee-adjusted signals ----
    signals = []
    has_book = bool(quote and quote["bid"] is not None and quote["ask"] is not None)
    if quote is None and m:
        signals.append("NO_DATA")
    elif m and not has_book:
        signals.append("NO_LIQUIDITY")
    edge_fee, breakeven, fee_c = None, None, None
    if fair is not None and has_book:
        ask, bid = quote["ask"], quote["bid"]
        fee_c = taker_fee_c(ask)
        breakeven = round(ask + fee_c / 100.0, 4)          # YES-long breakeven prob
        edge_fee = round(fair - breakeven, 4)
        no_p = 1 - bid
        edge_short_fee = round((bid - taker_fee_c(no_p) / 100.0) - fair, 4)
        spread = quote["spread"]
        ok_spread = spread is None or spread <= MAX_SPREAD
        if determined and ok_spread:
            if fair > 0.5 and edge_fee >= 0.10:
                signals.append("DETERMINED_YES")
            elif fair < 0.5 and edge_short_fee >= 0.10:
                signals.append("DETERMINED_NO")
        elif ok_spread:
            if edge_fee >= EDGE_THRESHOLD:
                signals.append("EDGE_LONG")
            elif edge_short_fee >= EDGE_THRESHOLD:
                signals.append("EDGE_SHORT")
    if model_split:
        signals.append("MODEL_SPLIT")
    if determined and not has_book:
        signals.append("WATCH_DETERMINED_YES" if fair > 0.5 else "WATCH_DETERMINED_NO")

    return {
        "city": city["code"], "name": city["name"], "date": datestr,
        "ticker": ticker,
        "expires_at": (day_start_utc + timedelta(hours=36)).isoformat(),
        "bid": quote["bid"] if quote else None, "ask": quote["ask"] if quote else None,
        "spread": round(quote["spread"], 3) if quote and quote["spread"] is not None else None,
        "volume": quote["volume"] if quote else None,
        "fair": fair,
        "edge_fee": edge_fee,
        "breakeven": breakeven, "fee_c": fee_c,
        "obs_in": round(obs_in, 3) if obs_in is not None else None,
        "obs_coverage": round(coverage, 2),
        "nws_pop_max": pop, "nws_qpf_mm": round(qpf_mm, 1) if qpf_mm is not None else None,
        "ens_p": round(p_ens, 3) if p_ens is not None else None,
        "det_mm": det_mm, "det_agree": det_agree, "model_split": model_split,
        "basis": basis, "signals": signals,
    }


def dormant_series_check():
    """Are the old RAIN* city series listing again?"""
    out = {}
    for s in ["RAINNY", "RAINMIA", "RAINSEA", "RAINHOU"]:
        try:
            d = get(f"{KALSHI}/events?series_ticker={s}&status=open&limit=5")
            evs = d.get("events", [])
            out[s] = [e["event_ticker"] for e in evs]
        except Exception:
            out[s] = None
    return out


def update_hotlist(results, now_mtn):
    """Contracts with a strong model lean but no usable book -> hotwatch targets."""
    try:
        hl = json.load(open(HOTLIST_PATH)) if os.path.exists(HOTLIST_PATH) else []
    except Exception:
        hl = []
    now_iso = now_mtn.isoformat()
    by_key = {e["key"]: e for e in hl if e.get("expires_at", "") > now_iso}
    for r in results:
        fair = r.get("fair")
        if fair is None or not r.get("ticker"):
            continue
        lean = fair - 0.5
        if abs(lean) < HOTLIST_LEAN:
            continue
        key = f"{r['city']}-{r['date']}"
        has_book = r.get("bid") is not None and r.get("ask") is not None
        if has_book and abs(r.get("edge_fee") or 0) >= 0.15:
            continue  # already actionable; the scan cron handles it
        by_key[key] = {
            "key": key, "city": r["city"], "name": r["name"], "date": r["date"],
            "ticker": r["ticker"], "fair": fair,
            "side": "long" if lean > 0 else "short",
            "added_at": now_iso, "expires_at": r["expires_at"],
        }
    hl = sorted(by_key.values(), key=lambda e: e["key"])
    os.makedirs(GOAL_DIR, exist_ok=True)
    with open(HOTLIST_PATH, "w") as f:
        json.dump(hl, f, indent=1)
    return hl


def main():
    cities = load_cities()
    now_mtn = datetime.now(ZoneInfo("America/Denver"))
    today = now_mtn.date()
    results, dormant = [], dormant_series_check()
    with ThreadPoolExecutor(max_workers=14) as ex:
        for is_today, day in [(True, today), (False, today + timedelta(days=1))]:
            ds = event_datestr(datetime(day.year, day.month, day.day))
            markets = kalshi_markets(ds)
            futs = [ex.submit(scan_city, c, markets, ds, is_today) for c in cities]
            for f in futs:
                try:
                    results.append(f.result())
                except Exception as e:
                    results.append({"city": "?", "signals": ["SCAN_ERROR"], "error": str(e)})
    hotlist = update_hotlist(results, now_mtn)
    payload = {"scanned_at": now_mtn.isoformat(), "results": results, "dormant_series": dormant,
               "hotlist": [e["key"] for e in hotlist]}
    with open(LATEST_PATH, "w") as f:
        json.dump(payload, f, indent=1)

    # console table
    print(f"{'city':5s} {'date':7s} {'book':>11s} {'fair':>6s} {'edge*':>6s} {'obs\"':>6s} {'ens':>5s} {'det':>5s}  signals")
    for r in sorted(results, key=lambda r: (r.get("date", ""), r.get("city", ""))):
        if r.get("bid") is not None and r.get("ask") is not None:
            book = f"{r['bid']:.2f}/{r['ask']:.2f}"
        else:
            book = "     --    "
        fair = f"{r['fair']:.2f}" if r.get("fair") is not None else "  --  "
        edge = f"{r['edge_fee']:+.2f}" if r.get("edge_fee") is not None else "  --  "
        obs = f"{r['obs_in']:.2f}" if r.get("obs_in") is not None else "  --  "
        ens = f"{r['ens_p']:.0%}" if r.get("ens_p") is not None else " -- "
        det = f"{r['det_agree']:.0%}" if r.get("det_agree") is not None else " -- "
        sig = ",".join(r.get("signals", []))
        print(f"{r.get('city','?'):5s} {r.get('date','?'):7s} {book:>11s} {fair:>6s} {edge:>6s} {obs:>6s} {ens:>5s} {det:>5s}  {sig}")
    print("\n* edge is fee-adjusted (Kalshi taker fee).")
    print("dormant RAIN* series open events:", {k: v for k, v in dormant.items() if v})
    print("hotlist:", [e["key"] for e in hotlist])
    return payload


if __name__ == "__main__":
    main()
