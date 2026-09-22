# Kalshi Weather Intelligence

A self-built AI-assisted research system that scans Kalshi's short-term weather
prediction markets (daily rain, daily high/low temperature) and compares live
market prices against meteorological data — National Weather Service
observations and forecasts plus a 31-member GFS ensemble — to find mispriced
contracts.

**Read-only by design.** This system analyzes public market data and publishes
research signals. It never places orders on its own. Any trade execution is a
separate, human-approved decision.

## What it does

- **Rain scanner** (`scan.py`) — covers 27 U.S. cities × today and tomorrow on
  Kalshi's `KXRAIN` daily markets. Fair value = mean of the 31-member GFS
  ensemble precipitation probability and deterministic model agreement.
- **Temperature scanner** (`temp_scan.py`) — covers 24 cities × daily high and
  low across ~360 laddered `KXHIGH` / `KXLOWT` contracts per run. Fair value =
  a normal distribution over the day's extreme, with the mean blending the NWS
  hourly extreme and the ensemble mean, sigma = max(ensemble std, 1.5°F).
- **Edge scoring** — every signal is fee-adjusted with Kalshi's taker-fee model
  (`ceil(7 × p × (1−p))` cents), so a quoted "edge" is net of what it costs to
  take the trade.
- **Determined-signal logic** — when observed station data already settles the
  question (e.g. 0.01" of rain already recorded), the system flags the contract
  as determined rather than priced.
- **Hot-watch** (`hotwatch.py`) — lightweight polling of bookmarked contracts;
  alerts only when a previously empty book gains quotes.

## The pipeline

`pipeline/` implements a 10-stage research loop (see `PIPELINE.md`):

1. Anomaly scanner → 2. Candidate promotion → 3. Settlement verification →
4. Base forecast → 5. Optional Level-2 review → 6. Exec-price/fee check →
7. **Bet proposal (human approval gate — never auto-executes)** →
8. Settlement tracking → 9. Append-only ledger → 10. Learning

Stage 7 is a propose-only gate: proposals require explicit human approval, and
live execution additionally requires a hand-created `LIVE_TRADING_ENABLED`
flag that no code path can create.

## The learning loop

Every scan is logged append-only. `pipeline/scoring.py` computes:

- **Determined-signal hit rate** — how often "determined" calls were right
- **Brier score** — probabilistic calibration of fair values vs outcomes
- **Per-city reliability** — which stations the model reads well, and which it doesn't

Open candidates are re-validated against each new scan and invalidated when
fair value moves against the thesis or the underlying observations revise.

## What the system has learned (calibration notes)

These are the findings that shaped the architecture — documented here because
they're the interesting part:

- **Observation revisions are real.** A station can report 0.06" of rain at
  5pm and revise to 0.00" by 9pm. The system invalidates determined signals on
  revision and applies a "thin margin" penalty (score × 0.85) when observed
  values sit near the trigger threshold.
- **Same-day gaps usually mean the model is wrong, not the market.** When a
  thick book disagrees with the model near expiry, the prior is a measurement
  mismatch (e.g. NWS station proxy vs the venue's settlement source), not free
  money. The system reports these as calibration discrepancies.
- **Station reporting cadence matters.** Some NWS stations report hourly,
  others every ~5 minutes. A fixed observation window silently misses most of
  the day at 5-minute stations — the scanners dedupe to one observation per
  clock hour and pull a full day's window.
- **Day-boundary conventions.** Overnight low markets can straddle calendar
  days; the model and the market may disagree on which "day" a reading belongs
  to. Flagged, not traded.

## Setup

```bash
pip install requests numpy
python3 scan.py        # rain markets
python3 temp_scan.py   # high/low temperature markets
python3 pipeline/run.py  # full scan -> candidates -> scoring loop
```

All data sources are public: Kalshi's public market API, api.weather.gov, and
GFS ensemble output. No API keys required.

## Status

Active research project, 2026. The weather slice is one component of a larger
prediction-market learning system; other market verticals are developed
separately.

---

*Research tooling for informational purposes. Model signals are not financial advice.*
