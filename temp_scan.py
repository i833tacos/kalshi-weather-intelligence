#!/usr/bin/env python3
"""
Kalshi daily high/low temperature scanner (KXHIGH* / KXLOWT*).

Per city per event day, Kalshi lists a ladder of markets on the day's
max (KXHIGH) or min (KXLOWT) temperature: "greater T", "less T", and
"between a-b" brackets tiling the plausible range. Settlement is via
The Weather Company; this scanner models against the NWS proxy
(hourly forecast + station obs + GFS 31-member ensemble spread).

Fair value per market, assuming the day's extreme ~ Normal(mu, sigma):
    mu    = mean(NWS hourly extreme, GFS-ensemble mean extreme)
    sigma = max(GFS-ensemble std of the extreme, 1.5F)
    greater T : 1 - Phi((T - mu)/sigma)
    less T    :     Phi((T - mu)/sigma)
    between   :     Phi((b - mu)/sigma) - Phi((a - mu)/sigma)

Determined logic (today only): the day's extreme is monotonic, so once
the observed station extreme already decides a market it is flagged
DETERMINED (0.99/0.01). Remaining-hour forecast + 2*sigma decides the
other side.

Edges are FEE-ADJUSTED with Kalshi's taker formula (same as scan.py).

Signals mirror the rain scanner:
  DETERMINED_YES / DETERMINED_NO, EDGE_LONG / EDGE_SHORT,
  WATCH_DETERMINED_YES / WATCH_DETERMINED_NO (determined, no book),
  TEMP_SPLIT (NWS hourly vs ensemble disagree > 4F),
  NO_LIQUIDITY, NO_DATA.

Markets with |fair - 0.5| >= 0.15 and no usable book go to
temp_hotlist.json, polled by hotwatch.py every few minutes.
Every scan appends to pipeline_state/temp_signal_history.jsonl
(model_version "temp-v1") so the temp brain can be scored like rain.
Read-only wrt Kalshi: never places orders.
"""
import json, math, os, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from scan import get, taker_fee_c  # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GOAL_DIR = os.path.expanduser("~/workspace/goals/kalshi-rain-watch/hidden_files")
STATE_PATH = os.path.join(GOAL_DIR, "temp_state.json")
LATEST_PATH = os.path.join(HERE, "temp_latest.json")
HOTLIST_PATH = os.path.join(GOAL_DIR, "temp_hotlist.json")
HISTORY_PATH = os.path.join(HERE, "pipeline_state", "temp_signal_history.jsonl")
MODEL_VERSION = "temp-v1"

EDGE_THRESHOLD = 0.12
MAX_SPREAD = 0.25
HOTLIST_LEAN = 0.15
MIN_SIGMA = 1.5
SPLIT_F = 4.0

# series suffix -> (cities.json code or None if new)
SERIES_CITIES = {
    "NY": "NYC", "EWR": "EWR", "DC": "DC", "PHIL": "PHIL", "BOS": "BOS",
    "TTN": "TTN", "SDF": None, "CHI": "CHI", "MIN": "MIN", "DEN": "DEN",
    "OKC": "OKC", "DAL": "DAL", "SATX": "SATX", "HOU": "HOU", "AUS": "AUS",
    "NOLA": "NOLA", "MIA": "MIA", "PHX": "PHX", "LV": "LV", "LAX": "LAX",
    "SAN": None, "SFO": "SFO", "SEA": "SEA", "TATL": "ATL",
}
EXTRA_CITIES = {
    "SAN": {"code": "SAN", "lat": 32.733, "lon": -117.183, "name": "San Diego",
            "station": "KSAN", "tz": "America/Los_Angeles"},
    "SDF": {"code": "SDF", "lat": 38.174, "lon": -85.736, "name": "Louisville",
            "station": "KSDF", "tz": "America/New_York"},
}


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0 if c is not None else None


def load_cities():
    base = {c["code"]: c for c in json.load(open(os.path.join(HERE, "cities.json")))["cities"]}
    base.update(EXTRA_CITIES)
    out = []
    for suffix, code in SERIES_CITIES.items():
        key = code or suffix
        c = dict(base[key])
        c["series"] = suffix
        out.append(c)
    return out


def event_datestr(dt):
    return dt.strftime("%y%b%d").upper()


# ---------------- Kalshi ----------------
def kalshi_event_markets(series, datestr):
    """All markets (with nested quotes) for e.g. KXHIGHNY-26SEP22."""
    try:
        d = get(f"{KALSHI}/events/{series}-{datestr}?with_nested_markets=true")
        return d.get("event", {}).get("markets", [])
    except Exception:
        return []


def parse_quote(m):
    def dollars(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None
    bid, ask = dollars(m.get("yes_bid_dollars")), dollars(m.get("yes_ask_dollars"))
    return {
        "bid": bid, "ask": ask,
        "spread": (ask - bid) if bid is not None and ask is not None else None,
        "volume": m.get("volume_fp"), "status": m.get("status"),
    }


# ---------------- NWS ----------------
def nws_hourly_extreme(lat, lon, day_start_utc, day_end_utc, hilo):
    """NWS hourly forecast max/min temp (F) over the event day; also remaining-day extreme."""
    try:
        pt = get(f"https://api.weather.gov/points/{lat},{lon}")["properties"]
        fc = get(pt["forecastHourly"])["properties"]["periods"]
    except Exception:
        return None, None
    now_utc = datetime.now(timezone.utc)
    vals, rem = [], []
    for p in fc:
        try:
            ts = datetime.fromisoformat(p["startTime"])
        except Exception:
            continue
        if not (day_start_utc <= ts < day_end_utc):
            continue
        t = p.get("temperature")
        if t is None:
            continue
        vals.append(t)
        if ts >= now_utc - timedelta(minutes=30):
            rem.append(t)
    if not vals:
        return None, None
    pick = max if hilo == "HIGH" else min
    return pick(vals), (pick(rem) if rem else None)


def nws_obs_extreme(station, day_start_utc, now_utc, hilo):
    """Observed max/min temp (F) since local midnight from the airport station.

    NOTE: some stations report every 5 min, others hourly. limit=400 covers a
    full day even at 5-min cadence; max/min over all obs is cadence-agnostic.
    """
    try:
        d = get(f"https://api.weather.gov/stations/{station}/observations?limit=400")
    except Exception:
        return None
    vals = []
    for f in d.get("features", []):
        p = f["properties"]
        try:
            ts = datetime.fromisoformat(p["timestamp"])
        except Exception:
            continue
        if ts < day_start_utc or ts > now_utc:
            continue
        t = c_to_f((p.get("temperature") or {}).get("value"))
        if t is not None:
            vals.append(t)
    if not vals:
        return None
    return max(vals) if hilo == "HIGH" else min(vals)


# ---------------- Open-Meteo ensemble ----------------
def ensemble_extreme(lat, lon, day_start_utc, day_end_utc, hilo):
    """Mean/std across 31 GFS members of the day's max (HIGH) or min (LOW) temp, in F."""
    try:
        d = get("https://ensemble-api.open-meteo.com/v1/ensemble?latitude=%.3f&longitude=%.3f"
                "&hourly=temperature_2m&models=gfs_seamless&forecast_days=3&timezone=UTC" % (lat, lon))
    except Exception:
        return None, None
    h = d.get("hourly", {})
    times = h.get("time", [])
    idx = []
    for i, t in enumerate(times):
        try:
            ts = datetime.fromisoformat(t)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if day_start_utc <= ts < day_end_utc:
            idx.append(i)
    if not idx:
        return None, None
    members = [k for k in h if k.startswith("temperature_2m_member")]
    extremes = []
    for k in members:
        vals = h[k]
        if len(vals) > max(idx):
            w = [c_to_f(vals[i]) for i in idx if vals[i] is not None]
            if w:
                extremes.append(max(w) if hilo == "HIGH" else min(w))
    if len(extremes) < 10:
        return None, None
    return mean(extremes), max(pstdev(extremes), MIN_SIGMA)


# ---------------- fair + determined ----------------
def market_fair(m, mu, sigma):
    st = m.get("strike_type")
    if st == "greater":
        return 1.0 - phi(((m.get("floor_strike") or 0) - mu) / sigma)
    if st == "less":
        return phi(((m.get("cap_strike") or 0) - mu) / sigma)
    if st == "between":
        a, b = m.get("floor_strike") or 0, m.get("cap_strike") or 0
        return phi((b - mu) / sigma) - phi((a - mu) / sigma)
    return None


def strike_label(m):
    st = m.get("strike_type")
    if st == "greater":
        return f">{m.get('floor_strike')}°"
    if st == "less":
        return f"<{m.get('cap_strike')}°"
    if st == "between":
        return f"{m.get('floor_strike')}-{m.get('cap_strike')}°"
    return "?"


def determined_outcome(m, obs, rem, sigma, hilo):
    """Return 'yes'/'no'/None: does the observed extreme (+ remaining forecast) decide it?"""
    st, margin = m.get("strike_type"), 2 * sigma
    if hilo == "HIGH":
        if st == "greater":
            T = m.get("floor_strike") or 0
            if obs is not None and obs > T:
                return "yes"
            if (obs is None or obs <= T) and rem is not None and rem + margin < T:
                return "no"
        elif st == "less":
            T = m.get("cap_strike") or 0
            if obs is not None and obs >= T:
                return "no"
            if (obs is None or obs < T) and rem is not None and rem + margin < T:
                return "yes"
        elif st == "between":
            a, b = m.get("floor_strike") or 0, m.get("cap_strike") or 0
            if obs is not None and obs > b:
                return "no"
            if obs is not None and a <= obs <= b and (rem is None or rem + margin <= b):
                return "yes"
            if (obs is None or obs < a) and rem is not None and rem + margin < a:
                return "no"
    else:  # LOW: observed min only falls; remaining can push it lower
        if st == "greater":
            T = m.get("floor_strike") or 0
            if obs is not None and obs <= T:
                return "no"
            if obs is not None and obs > T and rem is not None and rem - margin > T:
                return "yes"
        elif st == "less":
            T = m.get("cap_strike") or 0
            if obs is not None and obs < T:
                return "yes"
            if (obs is None or obs >= T) and rem is not None and rem - margin >= T:
                return "no"
        elif st == "between":
            a, b = m.get("floor_strike") or 0, m.get("cap_strike") or 0
            if obs is not None and obs < a:
                return "no"
            if obs is not None and a <= obs <= b and (rem is None or rem - margin >= a):
                return "yes"
            if (obs is None or obs > b) and rem is not None and rem - margin > b:
                return "no"
    return None


# ---------------- scan ----------------
def scan_city_hilo(city, hilo, datestr, is_today):
    tz = ZoneInfo(city["tz"])
    now_local = datetime.now(tz)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if not is_today:
        day_start_local += timedelta(days=1)
    day_start_utc = day_start_local.astimezone(timezone.utc)
    day_end_utc = (day_start_local + timedelta(days=1)).astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)

    series = f"KX{'HIGH' if hilo == 'HIGH' else 'LOWT'}{city['series']}"
    markets = kalshi_event_markets(series, datestr)

    nws_x, nws_rem = (None, None)
    obs = None
    if True:
        nws_x, nws_rem = nws_hourly_extreme(city["lat"], city["lon"], day_start_utc, day_end_utc, hilo)
        if is_today:
            obs = nws_obs_extreme(city["station"], day_start_utc, now_utc, hilo)
    ens_mu, ens_sigma = ensemble_extreme(city["lat"], city["lon"], day_start_utc, day_end_utc, hilo)

    mus = [v for v in (nws_x, ens_mu) if v is not None]
    mu = sum(mus) / len(mus) if mus else None
    sigma = max(ens_sigma or 0, MIN_SIGMA)
    temp_split = (nws_x is not None and ens_mu is not None and abs(nws_x - ens_mu) > SPLIT_F)

    rows = []
    for m in markets:
        ticker = m.get("ticker")
        if m.get("status") not in (None, "active"):
            continue
        q = parse_quote(m)
        has_book = q["bid"] is not None and q["ask"] is not None
        label = strike_label(m)

        fair, determined, det_side = None, False, None
        if mu is not None:
            fair = market_fair(m, mu, sigma)
            if is_today:
                det_side = determined_outcome(m, obs, nws_rem, sigma, hilo)
                if det_side:
                    fair, determined = (0.99 if det_side == "yes" else 0.01), True
            if not determined:
                if fair >= 0.985:
                    fair = 0.985
                elif fair <= 0.015:
                    fair = 0.015
            fair = round(max(0.005, min(0.995, fair)), 3)

        signals = []
        if mu is None:
            signals.append("NO_DATA")
        elif not has_book:
            signals.append("NO_LIQUIDITY")
        edge_fee, breakeven, fee_c = None, None, None
        if fair is not None and has_book:
            ask, bid = q["ask"], q["bid"]
            fee_c = taker_fee_c(ask)
            breakeven = round(ask + fee_c / 100.0, 4)
            edge_fee = round(fair - breakeven, 4)
            no_p = 1 - bid
            edge_short_fee = round((bid - taker_fee_c(no_p) / 100.0) - fair, 4)
            spread = q["spread"]
            ok_spread = spread is None or spread <= MAX_SPREAD
            if determined and ok_spread:
                if det_side == "yes" and edge_fee >= 0.10:
                    signals.append("DETERMINED_YES")
                elif det_side == "no" and edge_short_fee >= 0.10:
                    signals.append("DETERMINED_NO")
            elif ok_spread:
                if edge_fee >= EDGE_THRESHOLD:
                    signals.append("EDGE_LONG")
                elif edge_short_fee >= EDGE_THRESHOLD:
                    signals.append("EDGE_SHORT")
        if temp_split:
            signals.append("TEMP_SPLIT")
        if determined and not has_book:
            signals.append("WATCH_DETERMINED_YES" if det_side == "yes" else "WATCH_DETERMINED_NO")

        basis = []
        if mu is not None:
            parts = []
            if nws_x is not None:
                parts.append(f"NWS {nws_x:.0f}F")
            if ens_mu is not None:
                parts.append(f"ens {ens_mu:.0f}±{ens_sigma:.1f}F")
            basis.append("mu " + " / ".join(parts))
        if obs is not None:
            basis.append(f"obs {'max' if hilo == 'HIGH' else 'min'} {obs:.0f}F so far")
        if temp_split:
            basis.append(f"TEMP SPLIT: NWS {nws_x:.0f}F vs ens {ens_mu:.0f}F")

        rows.append({
            "city": city["code"], "series": city["series"], "hilo": hilo,
            "date": datestr, "ticker": ticker, "label": label,
            "strike_type": m.get("strike_type"),
            "strike": [m.get("floor_strike"), m.get("cap_strike")],
            "bid": q["bid"], "ask": q["ask"],
            "spread": round(q["spread"], 3) if q["spread"] is not None else None,
            "fair": fair, "edge_fee": edge_fee, "breakeven": breakeven, "fee_c": fee_c,
            "mu_f": round(mu, 1) if mu is not None else None,
            "sigma_f": round(sigma, 2),
            "nws_f": round(nws_x, 1) if nws_x is not None else None,
            "ens_mu_f": round(ens_mu, 1) if ens_mu is not None else None,
            "obs_f": round(obs, 1) if obs is not None else None,
            "determined": determined, "det_side": det_side,
            "temp_split": temp_split, "basis": basis, "signals": signals,
            "model_version": MODEL_VERSION,
        })
    return rows


def update_temp_hotlist(rows, now_utc):
    try:
        hl = json.load(open(HOTLIST_PATH)) if os.path.exists(HOTLIST_PATH) else []
    except Exception:
        hl = []
    now_iso = now_utc.isoformat()
    by_key = {e["key"]: e for e in hl if e.get("expires_at", "") > now_iso}
    for r in rows:
        fair = r.get("fair")
        if fair is None or not r.get("ticker"):
            continue
        lean = fair - 0.5
        if abs(lean) < HOTLIST_LEAN:
            continue
        key = f"T-{r['city']}-{r['hilo']}-{r['date']}-{r['ticker'].rsplit('-', 1)[-1]}"
        has_book = r.get("bid") is not None and r.get("ask") is not None
        if has_book and abs(r.get("edge_fee") or 0) >= 0.15:
            continue  # already actionable; the scan cron handles it
        hl_name = "high" if r["hilo"] == "HIGH" else "low"
        by_key[key] = {
            "key": key, "kind": "temp", "city": r["city"], "name": r.get("name"),
            "date": r["date"], "hilo": r["hilo"], "ticker": r["ticker"], "fair": fair,
            "side": "long" if lean > 0 else "short",
            "label": f"{r['city']} {hl_name} {r['label']} {r['date']}",
            "added_at": now_iso,
            "expires_at": (now_utc + timedelta(hours=36)).isoformat(),
        }
    hl = sorted(by_key.values(), key=lambda e: e["key"])
    os.makedirs(GOAL_DIR, exist_ok=True)
    with open(HOTLIST_PATH, "w") as f:
        json.dump(hl, f, indent=1)
    return hl


def log_history(rows, scanned_at):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "a") as f:
        for r in rows:
            f.write(json.dumps({
                "scanned_at": scanned_at, "city": r["city"], "hilo": r["hilo"],
                "date": r["date"], "ticker": r["ticker"], "label": r["label"],
                "strike_type": r["strike_type"], "strike": r["strike"],
                "fair": r["fair"], "edge_fee": r["edge_fee"],
                "bid": r["bid"], "ask": r["ask"],
                "mu_f": r["mu_f"], "sigma_f": r["sigma_f"],
                "obs_f": r["obs_f"], "determined": r["determined"],
                "signals": r["signals"], "model_version": MODEL_VERSION,
            }) + "\n")


def main():
    cities = load_cities()
    now_utc = datetime.now(timezone.utc)
    rows = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = []
        for c in cities:
            tz = ZoneInfo(c["tz"])
            now_local = datetime.now(tz)
            for is_today, hilo in [(True, "HIGH"), (True, "LOW"), (False, "HIGH"), (False, "LOW")]:
                day = now_local.date() + timedelta(days=0 if is_today else 1)
                ds = event_datestr(datetime(day.year, day.month, day.day))
                futs.append(ex.submit(scan_city_hilo, c, hilo, ds, is_today))
        for f in futs:
            try:
                rows.extend(f.result())
            except Exception as e:
                rows.append({"city": "?", "signals": ["SCAN_ERROR"], "error": str(e)})
    # attach names
    names = {c["code"]: c["name"] for c in cities}
    for r in rows:
        r["name"] = names.get(r.get("city"), r.get("city"))

    hotlist = update_temp_hotlist(rows, now_utc)
    scanned_at = now_utc.isoformat()
    log_history(rows, scanned_at)
    payload = {"scanned_at": scanned_at, "model_version": MODEL_VERSION,
               "rows": rows, "hotlist": [e["key"] for e in hotlist]}
    with open(LATEST_PATH, "w") as f:
        json.dump(payload, f, indent=1)

    print(f"{'city':5s} {'hl':4s} {'date':7s} {'strike':8s} {'book':>11s} {'fair':>6s} {'edge*':>6s} {'mu':>5s} {'sig':>4s}  signals")
    for r in sorted(rows, key=lambda r: (str(r.get("date")), str(r.get("city")), str(r.get("hilo")), str(r.get("label")))):
        if r.get("bid") is not None and r.get("ask") is not None:
            book = f"{r['bid']:.2f}/{r['ask']:.2f}"
        else:
            book = "     --    "
        fair = f"{r['fair']:.2f}" if r.get("fair") is not None else "  --  "
        edge = f"{r['edge_fee']:+.2f}" if r.get("edge_fee") is not None else "  --  "
        mu = f"{r['mu_f']:.0f}" if r.get("mu_f") is not None else "  -- "
        sig = f"{r['sigma_f']:.1f}" if r.get("sigma_f") is not None else " -- "
        print(f"{str(r.get('city')):5s} {str(r.get('hilo')):4s} {str(r.get('date')):7s} {str(r.get('label')):8s} {book:>11s} {fair:>6s} {edge:>6s} {mu:>5s} {sig:>4s}  {','.join(r.get('signals', []))}")
    print("\n* edge is fee-adjusted (Kalshi taker fee).")
    print("hotlist:", [e["key"] for e in hotlist])
    return payload


if __name__ == "__main__":
    main()
