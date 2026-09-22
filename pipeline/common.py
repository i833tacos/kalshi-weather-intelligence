#!/usr/bin/env python3
"""Shared paths, schemas, and helpers for the kalshi-rain pipeline.

All state lives under <repo>/pipeline_state/ so the whole thing is portable:
zip the kalshi-rain directory and it runs on any machine.
"""
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))   # pipeline/
ROOT = os.path.dirname(HERE)                         # kalshi-rain/
STATE = os.path.join(ROOT, "pipeline_state")

PATHS = {
    "scan_latest":    os.path.join(ROOT, "latest.json"),      # stage 1 output
    "candidates":     os.path.join(STATE, "candidates.json"),
    "settlement_map": os.path.join(STATE, "settlement_map.json"),
    "forecasts":      os.path.join(STATE, "forecasts.json"),
    "level2":         os.path.join(STATE, "level2.json"),
    "exec_checks":    os.path.join(STATE, "exec_checks.json"),
    "proposals":      os.path.join(STATE, "proposals.json"),
    "positions":      os.path.join(STATE, "positions.json"),
    "settlements":    os.path.join(STATE, "settlements.json"),
    "ledger":         os.path.join(STATE, "ledger.jsonl"),
    "scores":         os.path.join(STATE, "scores.json"),
}

# Bump when the fair-value formula changes; the ledger records it on every
# event so stage 10 can score model versions against each other.
MODEL_VERSION = "base-v1"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def record_ledger(event_type, **fields):
    """Append one event to the append-only ledger (stage 9)."""
    entry = {"ts": now_iso(), "type": event_type,
             "model_version": MODEL_VERSION}
    entry.update(fields)
    os.makedirs(STATE, exist_ok=True)
    with open(PATHS["ledger"], "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def ledger_tail(n=20, event_type=None):
    """Read the last n ledger entries (optionally filtered by type)."""
    try:
        with open(PATHS["ledger"]) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if event_type and e.get("type") != event_type:
            continue
        out.append(e)
        if len(out) >= n:
            break
    return list(reversed(out))
