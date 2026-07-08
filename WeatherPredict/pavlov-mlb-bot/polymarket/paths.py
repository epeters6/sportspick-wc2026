"""Filesystem paths for Polymarket MLB state (under project ``logs/`` / ``data/``)."""

from __future__ import annotations

import os

import data_paths as dp

_LOGS = dp.logs_dir()
_DATA = dp.data_dir()

POSITIONS       = os.path.join(_LOGS, "positions.json")
SIGNALS         = os.path.join(_LOGS, "signals.json")
STATION_SCORES  = os.path.join(_LOGS, "team_scores.json")
FORECAST_ERRORS = os.path.join(_LOGS, "forecast_errors.json")
ENSEMBLE_BIAS   = os.path.join(_LOGS, "ensemble_bias_poly.json")
ENSEMBLE_CACHE  = os.path.join(_DATA, "ensemble_cache_poly.json")
PENDING_SIGNALS = os.path.join(_DATA, "pending_poly_signals.json")
PENDING_MLB_POLY = os.path.join(_DATA, "pending_mlb_poly_signals.json")
