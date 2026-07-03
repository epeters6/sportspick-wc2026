"""Run the Kalshi signal_engine against Polymarket markets using isolated data files."""

from __future__ import annotations

import logging

import pipeline.calibration_log as calibration_log
import pipeline.ensemble_client as ensemble_client
import pipeline.signal_engine as signal_engine

from polymarket import paths as poly_paths

logger = logging.getLogger(__name__)


def get_all_signals(markets: list[dict], bankroll: float) -> list[dict]:
    """Same logic as Kalshi ``get_all_signals``, separate ensemble/bias/scores/logs."""
    with ensemble_client.isolated_storage(poly_paths.ENSEMBLE_CACHE, poly_paths.ENSEMBLE_BIAS):
        with calibration_log.use_forecast_errors_file(poly_paths.FORECAST_ERRORS):
            with signal_engine.use_station_score_paths(
                poly_paths.STATION_SCORES,
                poly_paths.POSITIONS,
            ):
                return signal_engine.get_all_signals(markets, bankroll)
