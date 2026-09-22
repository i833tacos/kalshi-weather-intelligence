#!/usr/bin/env python3
"""
Stage 4: base forecast (extract from scan.py).

Today the fair-value blend (GFS 31-member ensemble P + deterministic
agreement, determined-yes/no overrides) lives inside scan_city(). This stage
extracts it into a versioned module so:
  - the formula has a MODEL_VERSION stamped on every forecast,
  - stage 10 can score versions against each other,
  - level-2 can override the base fair without touching the scanner.

Contract:
  In:  candidate
  Out: pipeline_state/forecasts.json[{candidate_key}] =
       {fair, components: {ens_p, det_agree, ...}, model_version, at}
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MODEL_VERSION  # noqa: E402


def base_forecast(candidate):
    raise NotImplementedError(
        "stage4: extract fair-value blend from scan.py::scan_city into "
        f"this module (model_version={MODEL_VERSION}).")


if __name__ == "__main__":
    print("stage4_forecast: not built yet — blend still lives in scan.py.")
