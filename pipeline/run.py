#!/usr/bin/env python3
"""
Pipeline orchestrator. Runs the built stages in order:

  1. scan.py          (cheap anomaly scanner, ~30s)
  2. stage2_candidates (promote qualifiers -> candidates.json)

Later stages run per-candidate, on demand or via their own crons, as they
get built. Safe to run repeatedly: stage 2 dedupes.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def main():
    print("== stage 1: scan ==")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scan.py")],
                       cwd=ROOT)
    if r.returncode != 0:
        print("stage 1 failed, aborting")
        return 1
    print("== stage 2: candidates ==")
    from stage2_candidates import promote
    promote()
    print("== scoring: log + refresh ==")
    from scoring import log_scan, refresh_candidates
    log_scan()
    refresh_candidates()
    print("done. next: stage 3 (settlement verification) per candidate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
