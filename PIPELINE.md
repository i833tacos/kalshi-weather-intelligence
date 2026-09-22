# Kalshi Rain Pipeline — the full loop

**Loop:** cheap anomaly scanner → candidate → settlement verification →
Base forecast → optional Level 2 analysis → executable-price/fee check →
bet → settlement → ledger → learning

All pipeline state lives under `kalshi-rain/pipeline_state/` (portable: zip the
`kalshi-rain/` directory and it runs anywhere). Stage 1 (`scan.py`) also keeps
writing its existing paths so the live crons are untouched.

## Stage contracts

| # | Stage | Script | In → Out | Cadence | Status |
|---|-------|--------|----------|---------|--------|
| 1 | Cheap anomaly scanner | `scan.py` | weather+Kalshi APIs → `latest.json`, `hotlist.json` | 30 min | **live** |
| 2 | Candidate promotion | `pipeline/stage2_candidates.py` | `latest.json` → `pipeline_state/candidates.json` | after each scan | **live** |
| 3 | Settlement verification | `pipeline/stage3_settlement.py` | candidate → settlement source mapped + verified (`settlement_map.json`) | per candidate | stub — next build |
| 4 | Base forecast | `pipeline/stage4_forecast.py` | candidate → `forecasts.json` (fair, model_version) | per candidate | stub (logic lives in scan.py today) |
| 5 | Level 2 analysis (optional) | `pipeline/stage5_level2.py` | candidate → `level2.json` (refined fair + confidence) | per candidate, on demand | stub |
| 6 | Executable-price/fee check | `pipeline/stage6_exec.py` | candidate → `exec_checks.json` (fill price, fee, slippage, executable edge) | per candidate | stub (hotwatch.py covers book-appearance today) |
| 7 | Bet | `pipeline/stage7_bet.py` | executable → `proposals.json` → human approval → order | per proposal | **propose-only gate live** |
| 8 | Settlement | `pipeline/stage8_settlement.py` | open positions → `settlements.json` (actual outcome) | per position | stub |
| 9 | Ledger | `pipeline/common.py::record_ledger` | every event → `pipeline_state/ledger.jsonl` (append-only) | always | **live** |
| 10 | Learning | `pipeline/stage10_learning.py` | ledger + settlements → `scores.json` (Brier, calibration, edge realization) | daily/weekly | stub |

## Data schemas

**candidate** (`candidates.json`, keyed `{CITY}-{DATE}`):
`key, city, name, date, ticker, fair, edge_fee, breakeven, fee_c, spread,
volume, obs_in, ens_p, det_agree, model_split, basis[], scan_signals[],
side, stage, status, model_version, promoted_at, expires_at`.
Status flow: `new → verified → analyzed → executable → proposed → filled →
settled | expired | rejected`.

**proposal** (`proposals.json`): `id, candidate_key, side, size_contracts,
limit_price, est_fee_c, max_loss_c, fair, edge_fee, model_version,
created_at, status (awaiting_approval → approved → placed → filled |
rejected | expired)`.

**ledger** (`ledger.jsonl`, append-only, one JSON per line): every stage
transition plus `order_placed`, `order_filled`, `settled`. Always carries
`model_version` so learning can score versions against each other.

## Bet-stage safety rules (hard)

1. The pipeline **never places a live order on its own**. Stage 7 produces a
   proposal; a human approves each one explicitly.
2. `place_order()` refuses unless **both** hold: the proposal is
   `approved`, **and** `pipeline_state/LIVE_TRADING_ENABLED` exists (created
   by hand, never by code).
3. No auto-betting without the operator explicitly opting in with: max stake/trade,
   max daily loss, min edge, kill-switch. Not implemented until he asks.

## Why stage 3 (settlement verification) is the next build

Every edge we compute assumes our "observed rain" equals what settles the
market. KXRAIN settles off The Weather Company data for a specific
station/airport per city — not the NWS station we poll. If those disagree
(station siting, reporting lag, trace handling), our DETERMINED_YES edges are
fiction. Stage 3 must: pull each market's rulebook/settlement terms from the
Kalshi API, map city → exact settlement station, and cross-check NWS obs vs
the settlement feed on past days.

## Open questions

- Bet mode: stay propose-only (recommended), or define auto-bet limits?
- Default size per trade when proposing?
- Which Kalshi account funds this (for later API wiring)?
