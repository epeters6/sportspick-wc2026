"""Filesystem paths for the isolated Polymarket US pipeline."""

from __future__ import annotations

import os

import data_paths as dp

_LOGS_POLY  = dp.logs_poly_dir()
_DATA_POLY  = dp.data_poly_dir()

POSITIONS       = os.path.join(_LOGS_POLY, "positions.json")
SIGNALS         = os.path.join(_LOGS_POLY, "signals.json")
STATION_SCORES  = os.path.join(_LOGS_POLY, "station_scores.json")
FORECAST_ERRORS = os.path.join(_LOGS_POLY, "forecast_errors.json")
ENSEMBLE_BIAS   = os.path.join(_LOGS_POLY, "ensemble_bias.json")
ENSEMBLE_CACHE  = os.path.join(_DATA_POLY, "ensemble_cache_poly.json")
PENDING_SIGNALS = os.path.join(_DATA_POLY, "pending_signals.json")
