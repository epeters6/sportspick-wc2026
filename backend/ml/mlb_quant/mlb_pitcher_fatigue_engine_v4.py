"""
MLB Pitcher Fatigue Analytics Engine - v4.1
===========================================

Key upgrades over v3.0:
  1) Pitcher-specific volatility calibration:
     - Per-pitcher/per-pitch-type adaptive mean+sigma via EWMA stats.
     - Z-score based plausibility gates replace hard mph-only sigma gates.

  2) Bayesian + volume-weighted recovery:
     - Recovery quality is scored per pitch in [0, 1] from multi-metric z-scores.
     - Recovery posterior P(good mechanics) is updated with Beta-Binomial logic.
     - CUSUM decays by quality + volume (not a fixed constant).

  3) ML-style under probability model:
     - Logistic edge model turns state/context features into P(UNDER).
     - Online self-learning hook: resolved bets update model weights.

  4) Decision quality improvements:
     - Adaptive CUSUM sensitivity by live volatility.
     - Stronger hook logic and probability-gated lock-in.
     - Better dedupe and safer data guards.
  5) Dual-direction outs model:
      - Separate UNDER and OVER learners with distinct feature spaces.
      - Conservative no-bet edge filter with early-sample protection.
      - Backward-compatible migration from legacy single-model state.
  6) Online standardized coefficients:
      - Feature vectors are normalized with running z-score statistics.
      - Warmup+ramp blending preserves legacy behavior while converging to z-space.
      - Stable online SGD with gradient clipping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiohttp
import numpy as np
import pandas as pd
import pytz


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("engine.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def _fallback_lock_in_ping(
    name: str,
    prop: str,
    line: float,
    reason: str,
    side: Optional[str] = None,
    **_: Any,
) -> None:
    logger.info(
        "discord_alerts.send_lock_in_ping unavailable. Fallback alert: %s | %s %.1f | side=%s\n%s",
        name,
        prop,
        float(line),
        side or "?",
        reason,
    )


try:
    from discord_alerts import send_lock_in_ping  # type: ignore
except Exception:
    send_lock_in_ping = _fallback_lock_in_ping


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_QUANT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = _QUANT_DIR / "manifest.json"
MODEL_STATE_PATH = _QUANT_DIR / "under_model_state.json"
PENDING_PREDICTIONS_PATH = _QUANT_DIR / "pending_predictions.json"

POLL_INTERVAL_SECONDS = 30
MANIFEST_RELOAD_MIN_SECONDS = 5
MIN_PITCHES_BEFORE_TRACKING = 18
MIN_OUTS_BEFORE_EVALUATION = 9
MIN_OUTS_NEEDED_TO_BET = 2.5

# CUSUM in z-space
CUSUM_BASE_K = 0.55
CUSUM_BASE_H = 4.8
VOTE_THRESHOLD = 2

# Adaptive volatility model
ADAPTIVE_WARMUP_PITCHES = 30
ADAPTIVE_ALPHA = 0.08
ADAPTIVE_SIGMA_FLOOR_MULT = 0.45
Z_OUTLIER_LIMIT = 5.5
SPEED_Z_OUTLIER_LIMIT = 5.0
ADAPTIVE_UPDATE_MIN_QUALITY = 0.60

# Bayesian recovery
RECOVERY_PRIOR_ALPHA = 2.0
RECOVERY_PRIOR_BETA = 2.0
RECOVERY_MIN_PITCHES = 5
RECOVERY_MAX_PITCHES = 10
RECOVERY_MIN_EFFECTIVE_VOLUME = 3.5
RECOVERY_POSTERIOR_THRESHOLD = 0.70
RECOVERY_FAIL_POSTERIOR = 0.42
RECOVERY_CUSUM_UNLOCK = 1.8
RECOVERY_BASE_DECAY = 0.15
RECOVERY_QUALITY_DECAY_MULT = 0.60
RECOVERY_VOLUME_DECAY_MULT = 0.25

# Probability model / decision thresholds
MODEL_LEARNING_RATE = 0.08
MODEL_L2 = 0.001
MODEL_GRAD_CLIP = 6.0
FEATURE_STANDARDIZER_MIN_SAMPLES = 24
FEATURE_STANDARDIZER_RAMP_SAMPLES = 96
FEATURE_STANDARDIZER_SIGMA_FLOOR = 0.05
FEATURE_STANDARDIZER_Z_CLIP = 6.0
DEFAULT_RUN_MODE = "training"
NO_BET_PROFILE_BY_MODE: dict[str, dict[str, float]] = {
    # Looser profile for paper trading to collect more labels.
    "training": {
        "side_proba_mature": 0.56,
        "edge_gap_mature": 0.04,
        "side_proba_early": 0.55,
        "edge_gap_early": 0.03,
        "model_maturity_updates": 80.0,
        "under_min_outs_needed": 1.25,
        "over_max_outs_needed": 10.0,
        "over_pitch_count_buffer": 4.0,
        "under_model_only_edge_bonus": 0.02,
    },
    # Tighter profile for real-money deployment.
    "live": {
        "side_proba_mature": 0.58,
        "edge_gap_mature": 0.07,
        "side_proba_early": 0.61,
        "edge_gap_early": 0.10,
        "model_maturity_updates": 40.0,
        "under_min_outs_needed": MIN_OUTS_NEEDED_TO_BET,
        "over_max_outs_needed": 8.5,
        "over_pitch_count_buffer": 8.0,
        "under_model_only_edge_bonus": 0.04,
    },
}

# Pitch count ceilings by tier
PC_CEILING = {1: 105, 2: 95, 3: 88}

PITCH_FAMILIES: dict[str, list[str]] = {
    "fastball": ["4-Seam Fastball", "Two-Seam Fastball", "Sinker", "Cutter"],
    "breaking": ["Slider", "Curveball", "Knuckle Curve", "Sweeper"],
    "offspeed": ["Changeup", "Splitter", "Forkball"],
}
FAMILY_LOOKUP: dict[str, str] = {
    pitch: fam for fam, pitches in PITCH_FAMILIES.items() for pitch in pitches
}

METRICS = [
    "release_speed",
    "release_pos_z",
    "release_pos_x",
    "release_spin_rate",
    "release_extension",
]

DEFAULT_SIGMA_BY_METRIC: dict[str, float] = {
    "release_speed": 1.5,
    "release_pos_z": 0.18,
    "release_pos_x": 0.22,
    "release_spin_rate": 120.0,
    "release_extension": 0.18,
}

# For converting a metric's z-score into fatigue evidence.
# -1 means "drop = fatigue" (speed/spin/extension), 0 means "deviation either way = fatigue".
FATIGUE_DIRECTION: dict[str, int] = {
    "release_speed": -1,
    "release_spin_rate": -1,
    "release_extension": -1,
    "release_pos_z": 0,
    "release_pos_x": 0,
}

METRIC_WEIGHTS: dict[str, float] = {
    "release_speed": 0.35,
    "release_pos_z": 0.20,
    "release_pos_x": 0.15,
    "release_spin_rate": 0.20,
    "release_extension": 0.10,
}

LIVE_STRIKE_CALLS = {
    "called strike",
    "swinging strike",
    "swinging strike blocked",
    "foul",
    "foul tip",
    "foul bunt",
    "missed bunt",
    "in play, out(s)",
    "in play, no out",
    "in play, run(s)",
}
LIVE_WHIFF_CALLS = {
    "swinging strike",
    "swinging strike blocked",
    "missed bunt",
}

DEFAULT_UNDER_MODEL_WEIGHTS: dict[str, float] = {
    "bias": -1.15,
    "fatigue_vote_frac": 1.55,
    "avg_cusum_norm": 1.10,
    "pitch_count_ratio": 0.85,
    "outs_needed_norm": 0.75,
    "vel_drop_z": 0.55,
    "spin_drop_z": 0.35,
    "run_diff_neg": 0.40,
    "li_norm": 0.30,
    "inning_norm": 0.10,
    "near_ceiling": 0.55,
    "bullpen_fresh": 0.25,
    "hook_signal": 0.65,
    # New pregame/context features.
    "manager_hook_score": 0.50,
    "ttto_penalty": 0.45,
    "days_rest_short": 0.35,
    "rolling_pc_hot": 0.30,
    "velocity_decay_46": 0.38,
    "command_loss": 0.32,
    "opponent_contact": 0.28,
    "opp_work_rate": 0.22,
    "park_hr_factor": 0.24,
    "weather_fatigue": 0.15,
    "umpire_k_zone": -0.18,
    "line_move_delta_norm": -0.28,
    "prop_vs_expected_gap": 0.35,
    "fip_norm": 0.20,
    "barrel_rate_norm": 0.22,
    "hard_hit_rate_norm": 0.22,
    "gb_rate_norm": -0.08,
    "in_game_pitch_efficiency_stress": 0.26,
    "live_command_loss": 0.30,
    "live_p_pa_stress": 0.22,
    "live_behind_rate": 0.18,
    "opp_bb_rate": 0.24,
    "lineup_balance_risk": 0.05,
    "pinch_hit_risk": 0.14,
    "bullpen_fatigue_score": -0.20,
    "temperature_stress": 0.12,
    "humidity_stress": 0.08,
    "wind_stress": 0.06,
    "altitude_stress": 0.10,
    "line_move_abs_norm": -0.05,
    "market_spread_norm": -0.06,
    "last_move_up_norm": -0.10,
    "prop_line_level": 0.16,
    "outs_per_inning_needed_norm": 0.32,
    "o_swing_skill": -0.08,
    "behind_pct_norm": 0.16,
    "pitches_per_ip_stress": 0.14,
    "hr_per_9_norm": 0.12,
    "babip_norm": 0.10,
    "lob_strength": -0.08,
    "long_leash_history": -0.10,
    "home_manager_patience": -0.05,
    "early_signal_bonus": 0.08,
}

DEFAULT_OVER_MODEL_WEIGHTS: dict[str, float] = {
    "bias": -1.35,
    "stability_score": 1.05,
    "rebound_signal_norm": 0.90,
    "pitch_count_buffer": 1.10,
    "outs_needed_short_norm": 0.95,
    "run_diff_pos": 0.35,
    "li_low": 0.30,
    "inning_early": 0.35,
    "bullpen_taxed": 0.55,
    "hook_risk_low": 0.70,
    "vel_stability": 0.60,
    "spin_stability": 0.35,
    # New pregame/context features.
    "manager_hook_score": -0.45,
    "ttto_penalty": -0.30,
    "days_rest_fresh": 0.34,
    "rolling_pc_hot": -0.26,
    "velocity_decay_46": -0.24,
    "command_quality": 0.40,
    "opponent_k_rate": 0.24,
    "opp_work_rate": -0.20,
    "park_pitcher_friendly": 0.18,
    "weather_fatigue": -0.12,
    "umpire_k_zone": 0.16,
    "line_move_delta_norm": 0.26,
    "prop_vs_expected_gap": -0.34,
    "fip_norm": -0.18,
    "barrel_rate_norm": -0.20,
    "hard_hit_rate_norm": -0.20,
    "gb_rate_norm": 0.10,
    "in_game_pitch_efficiency_quality": 0.24,
    "live_command_quality": 0.30,
    "live_p_pa_quality": 0.22,
    "live_behind_rate": -0.18,
    "opp_bb_rate": -0.24,
    "lineup_balance_risk": -0.05,
    "pinch_hit_risk": -0.16,
    "bullpen_fatigue_score": 0.24,
    "temperature_stress": -0.10,
    "humidity_stress": -0.07,
    "wind_stress": -0.05,
    "altitude_stress": -0.08,
    "line_move_abs_norm": 0.04,
    "market_spread_norm": -0.07,
    "last_move_up_norm": 0.12,
    "prop_line_level": -0.18,
    "outs_per_inning_needed_norm": -0.34,
    "o_swing_skill": 0.10,
    "behind_pct_norm": -0.15,
    "pitches_per_ip_stress": -0.16,
    "hr_per_9_norm": -0.10,
    "babip_norm": -0.09,
    "lob_strength": 0.10,
    "long_leash_history": 0.12,
    "home_manager_patience": 0.05,
    "early_signal_bonus": 0.08,
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def sigmoid(x: float) -> float:
    x = clamp(x, -35.0, 35.0)
    return 1.0 / (1.0 + math.exp(-x))


def get_pitch_family(pitch_name: str) -> str:
    return FAMILY_LOOKUP.get(pitch_name, "unknown")


def bullpen_fresh_score(status: str) -> float:
    s = str(status or "").strip().lower()
    if s in {"fresh", "rested", "strong"}:
        return 1.0
    if s in {"neutral", "average", "ok"}:
        return 0.5
    if s in {"taxed", "burned", "gassed", "tired"}:
        return 0.0
    return 0.5


def normalize_run_mode(mode: Optional[str]) -> str:
    m = str(mode or "").strip().lower()
    if m in {"prod", "production"}:
        m = "live"
    if m not in {"training", "live"}:
        return DEFAULT_RUN_MODE
    return m


def get_no_bet_profile(run_mode: str) -> dict[str, float]:
    normalized = normalize_run_mode(run_mode)
    return dict(NO_BET_PROFILE_BY_MODE[normalized])


def pitch_quality_from_zscores(metric_z: dict[str, float]) -> float:
    """
    Converts multi-metric z-scores into a [0, 1] quality score.
    1.0 = very close to baseline, 0.0 = highly degraded/noisy.
    """
    if not metric_z:
        return 0.0
    weighted_sq = 0.0
    total_w = 0.0
    for metric, z in metric_z.items():
        w = METRIC_WEIGHTS.get(metric, 0.1)
        weighted_sq += w * (z ** 2)
        total_w += w
    if total_w <= 0:
        return 0.0
    mean_sq = weighted_sq / total_w
    return clamp(math.exp(-0.5 * mean_sq), 0.0, 1.0)


def fatigue_z_from_metric(metric: str, z_raw: float) -> float:
    """
    Maps raw z-score to fatigue evidence.
    Positive fatigue_z means evidence of mechanical decay.
    """
    direction = FATIGUE_DIRECTION.get(metric, 0)
    if direction < 0:
        return -z_raw
    if direction > 0:
        return z_raw
    return abs(z_raw)


# ---------------------------------------------------------------------------
# Adaptive metric stats
# ---------------------------------------------------------------------------


@dataclass
class AdaptiveMetricStats:
    prior_mean: float
    prior_sigma: float
    n: int = 0
    ewma_mean: Optional[float] = None
    ewma_var: Optional[float] = None

    def current_mean(self) -> float:
        if self.ewma_mean is None:
            return float(self.prior_mean)
        warm = min(1.0, self.n / ADAPTIVE_WARMUP_PITCHES)
        return float((1.0 - warm) * self.prior_mean + warm * self.ewma_mean)

    def current_sigma(self) -> float:
        prior_sigma = max(float(self.prior_sigma), 0.2)
        if self.ewma_var is None:
            return prior_sigma

        online_sigma = math.sqrt(max(self.ewma_var, 1e-6))
        warm = min(1.0, self.n / ADAPTIVE_WARMUP_PITCHES)
        blended = (1.0 - warm) * prior_sigma + warm * online_sigma
        return max(prior_sigma * ADAPTIVE_SIGMA_FLOOR_MULT, blended)

    def zscore(self, observed: float) -> float:
        sigma = self.current_sigma()
        if sigma <= 0:
            return 0.0
        return (float(observed) - self.current_mean()) / sigma

    def sigma_ratio(self) -> float:
        return self.current_sigma() / max(float(self.prior_sigma), 0.2)

    def update(self, observed: float, quality: float = 1.0) -> None:
        observed = float(observed)
        q = clamp(float(quality), 0.1, 1.0)
        alpha = ADAPTIVE_ALPHA * q

        if self.ewma_mean is None:
            self.ewma_mean = observed
            self.ewma_var = max(self.prior_sigma, 0.2) ** 2
            self.n = 1
            return

        diff = observed - self.ewma_mean
        new_mean = self.ewma_mean + alpha * diff
        new_var = (1.0 - alpha) * (self.ewma_var + alpha * (diff ** 2))

        self.ewma_mean = new_mean
        self.ewma_var = max(new_var, 1e-6)
        self.n += 1


# ---------------------------------------------------------------------------
# CUSUM trackers
# ---------------------------------------------------------------------------


@dataclass
class FatigueCUSUMTracker:
    k: float = CUSUM_BASE_K
    h: float = CUSUM_BASE_H
    fatigue_cusum: float = 0.0
    rebound_cusum: float = 0.0
    n_pitches: int = 0

    def update(self, fatigue_z: float, k_scale: float = 1.0, h_scale: float = 1.0) -> dict[str, Any]:
        k = self.k * k_scale
        h = self.h * h_scale

        self.n_pitches += 1
        self.fatigue_cusum = max(0.0, self.fatigue_cusum + fatigue_z - k)
        self.rebound_cusum = max(0.0, self.rebound_cusum - fatigue_z - k)

        fatigue_signal = self.fatigue_cusum > h
        rebound_signal = self.rebound_cusum > h

        return {
            "signal": fatigue_signal,
            "fatigue_signal": fatigue_signal,
            "rebound_signal": rebound_signal,
            "cusum_fatigue": round(self.fatigue_cusum, 3),
            "cusum_rebound": round(self.rebound_cusum, 3),
            "h_used": round(h, 3),
            "k_used": round(k, 3),
        }

    def decay(self, amount: float) -> None:
        self.fatigue_cusum = max(0.0, self.fatigue_cusum - amount)
        self.rebound_cusum = max(0.0, self.rebound_cusum - amount)

    def reset(self) -> None:
        self.fatigue_cusum = 0.0
        self.rebound_cusum = 0.0


# ---------------------------------------------------------------------------
# Recovery and ML model state
# ---------------------------------------------------------------------------


@dataclass
class BayesianRecoveryState:
    active: bool = False
    alpha: float = RECOVERY_PRIOR_ALPHA
    beta: float = RECOVERY_PRIOR_BETA
    weighted_volume: float = 0.0
    pitches_seen: int = 0

    @property
    def posterior_good(self) -> float:
        return self.alpha / max(self.alpha + self.beta, 1e-6)

    @property
    def effective_n(self) -> float:
        return self.alpha + self.beta

    def activate(self) -> None:
        self.active = True
        self.alpha = RECOVERY_PRIOR_ALPHA
        self.beta = RECOVERY_PRIOR_BETA
        self.weighted_volume = 0.0
        self.pitches_seen = 0

    def reset(self) -> None:
        self.active = False
        self.alpha = RECOVERY_PRIOR_ALPHA
        self.beta = RECOVERY_PRIOR_BETA
        self.weighted_volume = 0.0
        self.pitches_seen = 0

    def update(self, quality: float) -> None:
        q = clamp(float(quality), 0.0, 1.0)
        self.alpha += q
        self.beta += (1.0 - q)
        self.weighted_volume += q
        self.pitches_seen += 1


@dataclass
class OnlineFeatureStats:
    """
    Running feature moments via Welford updates.
    Tracks per-feature mean and variance used for z-score normalization.
    """

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def update(self, value: float) -> None:
        x = float(value)
        self.n += 1
        delta = x - self.mean
        self.mean += delta / max(self.n, 1)
        delta2 = x - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self) -> float:
        if self.n < 2:
            return 1.0
        return max(self.m2 / max(self.n - 1, 1), 1e-9)

    @property
    def sigma(self) -> float:
        return math.sqrt(self.variance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": int(self.n),
            "mean": float(self.mean),
            "m2": float(self.m2),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OnlineFeatureStats":
        stats = cls()
        stats.n = max(0, safe_int(payload.get("n"), 0))
        stats.mean = float(safe_float(payload.get("mean"), 0.0))
        stats.m2 = float(max(safe_float(payload.get("m2"), 0.0), 0.0))
        return stats


@dataclass
class OnlineFeatureStandardizer:
    """
    Z-score feature standardizer with warmup+ramp blending.
    During warmup the model sees mostly raw values (migration-safe for legacy raw weights),
    then smoothly transitions into full z-space as samples accumulate.
    """

    stats: dict[str, OnlineFeatureStats] = field(default_factory=dict)
    min_samples: int = FEATURE_STANDARDIZER_MIN_SAMPLES
    ramp_samples: int = FEATURE_STANDARDIZER_RAMP_SAMPLES
    sigma_floor: float = FEATURE_STANDARDIZER_SIGMA_FLOOR
    z_clip: float = FEATURE_STANDARDIZER_Z_CLIP

    def _get_or_create(self, feature_name: str) -> OnlineFeatureStats:
        if feature_name not in self.stats:
            self.stats[feature_name] = OnlineFeatureStats()
        return self.stats[feature_name]

    def update(self, features: dict[str, float]) -> None:
        for feat, value in features.items():
            x = safe_float(value, 0.0)
            if not math.isfinite(x):
                continue
            self._get_or_create(feat).update(x)

    def readiness_for(self, feature_name: str) -> float:
        stat = self.stats.get(feature_name)
        if stat is None or stat.n <= self.min_samples:
            return 0.0
        ramp = max(self.ramp_samples, 1)
        return clamp((stat.n - self.min_samples) / ramp, 0.0, 1.0)

    def transform_value(self, feature_name: str, value: float) -> float:
        x = safe_float(value, 0.0)
        if not math.isfinite(x):
            return 0.0

        stat = self.stats.get(feature_name)
        if stat is None or stat.n < 2:
            return x

        sigma = max(stat.sigma, self.sigma_floor)
        z = (x - stat.mean) / sigma
        z = clamp(z, -self.z_clip, self.z_clip)
        readiness = self.readiness_for(feature_name)
        # Smooth migration from raw-space to z-space.
        return ((1.0 - readiness) * x) + (readiness * z)

    def transform(self, features: dict[str, float]) -> dict[str, float]:
        return {
            feat: float(self.transform_value(feat, value))
            for feat, value in features.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_samples": int(self.min_samples),
            "ramp_samples": int(self.ramp_samples),
            "sigma_floor": float(self.sigma_floor),
            "z_clip": float(self.z_clip),
            "stats": {
                feat: stat.to_dict()
                for feat, stat in self.stats.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OnlineFeatureStandardizer":
        std = cls()
        std.min_samples = max(1, safe_int(payload.get("min_samples"), FEATURE_STANDARDIZER_MIN_SAMPLES))
        std.ramp_samples = max(1, safe_int(payload.get("ramp_samples"), FEATURE_STANDARDIZER_RAMP_SAMPLES))
        std.sigma_floor = max(1e-6, safe_float(payload.get("sigma_floor"), FEATURE_STANDARDIZER_SIGMA_FLOOR))
        std.z_clip = max(1.0, safe_float(payload.get("z_clip"), FEATURE_STANDARDIZER_Z_CLIP))

        raw_stats = payload.get("stats", {})
        if isinstance(raw_stats, dict):
            for feat, stat_payload in raw_stats.items():
                if not isinstance(feat, str) or not isinstance(stat_payload, dict):
                    continue
                std.stats[feat] = OnlineFeatureStats.from_dict(stat_payload)
        return std


@dataclass
class UnderProbabilityModel:
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_UNDER_MODEL_WEIGHTS))
    learning_rate: float = MODEL_LEARNING_RATE
    l2: float = MODEL_L2
    grad_clip: float = MODEL_GRAD_CLIP
    standardizer: OnlineFeatureStandardizer = field(default_factory=OnlineFeatureStandardizer)
    updates: int = 0

    def _coerce_features(self, features: dict[str, float]) -> dict[str, float]:
        return {feat: safe_float(value, 0.0) for feat, value in features.items()}

    def _predict_from_transformed(self, transformed_features: dict[str, float]) -> float:
        z = float(self.weights.get("bias", 0.0))
        for feat, value in transformed_features.items():
            z += float(self.weights.get(feat, 0.0)) * float(value)
        return sigmoid(z)

    def predict_proba(self, features: dict[str, float]) -> float:
        raw = self._coerce_features(features)
        transformed = self.standardizer.transform(raw)
        return self._predict_from_transformed(transformed)

    def update(self, features: dict[str, float], label_under: int) -> float:
        """
        Online logistic update after a game settles.
        label_under: 1 if UNDER hit, else 0.
        """
        raw = self._coerce_features(features)
        transformed = self.standardizer.transform(raw)
        y = float(label_under)
        pred = self._predict_from_transformed(transformed)
        err = pred - y
        step = self.learning_rate / math.sqrt(1.0 + 0.25 * self.updates)

        for feat, value in transformed.items():
            w = float(self.weights.get(feat, 0.0))
            grad = err * float(value) + self.l2 * w
            if self.grad_clip > 0:
                grad = clamp(grad, -self.grad_clip, self.grad_clip)
            self.weights[feat] = w - step * grad

        bias = float(self.weights.get("bias", 0.0))
        self.weights["bias"] = bias - step * err
        self.updates += 1
        # Update feature moments after gradient step to avoid target leakage into current transform.
        self.standardizer.update(raw)
        return pred

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "learning_rate": self.learning_rate,
            "l2": self.l2,
            "grad_clip": self.grad_clip,
            "standardizer": self.standardizer.to_dict(),
            "updates": self.updates,
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        default_weights: Optional[dict[str, float]] = None,
    ) -> "UnderProbabilityModel":
        base_weights = dict(default_weights or DEFAULT_UNDER_MODEL_WEIGHTS)
        model = cls()
        model.weights = {
            **base_weights,
            **{k: float(v) for k, v in (payload.get("weights") or {}).items()},
        }
        model.learning_rate = float(payload.get("learning_rate", MODEL_LEARNING_RATE))
        model.l2 = float(payload.get("l2", MODEL_L2))
        model.grad_clip = float(payload.get("grad_clip", MODEL_GRAD_CLIP))
        std_payload = payload.get("standardizer")
        if isinstance(std_payload, dict):
            model.standardizer = OnlineFeatureStandardizer.from_dict(std_payload)
        else:
            model.standardizer = OnlineFeatureStandardizer()
        model.updates = int(payload.get("updates", 0))
        return model


@dataclass
class DirectionalOutsModels:
    under: UnderProbabilityModel = field(default_factory=UnderProbabilityModel)
    over: UnderProbabilityModel = field(
        default_factory=lambda: UnderProbabilityModel(weights=dict(DEFAULT_OVER_MODEL_WEIGHTS))
    )

    @property
    def total_updates(self) -> int:
        return int(self.under.updates + self.over.updates)


# ---------------------------------------------------------------------------
# Per-pitcher runtime state
# ---------------------------------------------------------------------------


@dataclass
class PitcherState:
    p_id: str
    prediction: Optional[str] = None
    processed_pitch_ids: set[str] = field(default_factory=set)
    # cusum_trackers[pitch_type][metric]
    cusum_trackers: dict[str, dict[str, FatigueCUSUMTracker]] = field(default_factory=dict)
    # adaptive_stats[pitch_type][metric]
    adaptive_stats: dict[str, dict[str, AdaptiveMetricStats]] = field(default_factory=dict)
    recovery: BayesianRecoveryState = field(default_factory=BayesianRecoveryState)
    last_signal_details: dict[str, Any] = field(default_factory=dict)
    last_under_proba: float = 0.0
    last_over_proba: float = 0.0
    last_under_features: dict[str, float] = field(default_factory=dict)
    last_over_features: dict[str, float] = field(default_factory=dict)

    def get_tracker(self, pitch_type: str, metric: str) -> FatigueCUSUMTracker:
        if pitch_type not in self.cusum_trackers:
            self.cusum_trackers[pitch_type] = {}
        if metric not in self.cusum_trackers[pitch_type]:
            self.cusum_trackers[pitch_type][metric] = FatigueCUSUMTracker()
        return self.cusum_trackers[pitch_type][metric]

    def get_stat(self, pitch_type: str, metric: str, base: dict[str, Any]) -> AdaptiveMetricStats:
        if pitch_type not in self.adaptive_stats:
            self.adaptive_stats[pitch_type] = {}
        if metric not in self.adaptive_stats[pitch_type]:
            prior_mean = float(base.get(metric, 0.0))
            prior_sigma = float(base.get(f"{metric}_sigma", DEFAULT_SIGMA_BY_METRIC.get(metric, 1.0)))
            self.adaptive_stats[pitch_type][metric] = AdaptiveMetricStats(
                prior_mean=prior_mean,
                prior_sigma=prior_sigma,
            )
        return self.adaptive_stats[pitch_type][metric]

    def iter_all_trackers(self):
        for metric_map in self.cusum_trackers.values():
            for tracker in metric_map.values():
                yield tracker

    def avg_fatigue_cusum(self, pitch_type: Optional[str] = None) -> float:
        values: list[float] = []
        if pitch_type is not None:
            for tracker in self.cusum_trackers.get(pitch_type, {}).values():
                values.append(tracker.fatigue_cusum)
        else:
            for tracker in self.iter_all_trackers():
                values.append(tracker.fatigue_cusum)
        if not values:
            return 0.0
        return float(np.mean(values))


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4)
    os.replace(tmp_path, path)


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("Failed reading %s: %s", path, exc)
        return default


def save_json_file(path: Path, payload: Any) -> None:
    _atomic_write_json(path, payload)


def _manifest_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _extract_pitcher_manifest(raw_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cleaned: dict[str, dict[str, Any]] = {}
    for p_id, payload in raw_manifest.items():
        if isinstance(payload, dict) and "name" in payload and "baseline" in payload:
            cleaned[str(p_id)] = payload
    return cleaned


def _manifest_slate_date(manifest: dict[str, dict[str, Any]]) -> Optional[str]:
    counts: dict[str, int] = {}
    for payload in manifest.values():
        slate_date = payload.get("slate_date")
        if not slate_date:
            continue
        key = str(slate_date)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _load_manifest_with_retry(
    path: Path,
    retries: int = 3,
    retry_delay: float = 0.25,
) -> dict[str, dict[str, Any]]:
    for attempt in range(1, retries + 1):
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                return {}
            return _extract_pitcher_manifest(payload)
        except json.JSONDecodeError:
            if attempt == retries:
                logger.warning("Manifest JSON decode failed after %d attempts.", retries)
                return {}
            time.sleep(retry_delay)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Unexpected manifest read error on attempt %d: %s", attempt, exc)
            if attempt == retries:
                return {}
            time.sleep(retry_delay)
    return {}


def _is_new_slate(
    old_manifest: dict[str, dict[str, Any]],
    new_manifest: dict[str, dict[str, Any]],
) -> bool:
    old_ids = set(old_manifest.keys())
    new_ids = set(new_manifest.keys())
    if not old_ids or not new_ids:
        return False

    old_slate_date = _manifest_slate_date(old_manifest)
    new_slate_date = _manifest_slate_date(new_manifest)
    if old_slate_date and new_slate_date and old_slate_date != new_slate_date:
        return True

    overlap = len(old_ids & new_ids)
    union = len(old_ids | new_ids)
    overlap_ratio = overlap / max(union, 1)

    # Fallback when slate_date is missing: if roster mostly changed, treat as new slate.
    if overlap_ratio < 0.35 and len(new_ids) >= 6:
        return True
    return False


def _rebuild_states_for_manifest(
    manifest: dict[str, dict[str, Any]],
    old_states: dict[str, PitcherState],
    force_fresh: bool,
) -> dict[str, PitcherState]:
    new_states: dict[str, PitcherState] = {}
    for p_id, payload in manifest.items():
        if not force_fresh and p_id in old_states:
            state = old_states[p_id]
        else:
            state = PitcherState(p_id=p_id)

        # Respect persisted prediction if present on disk.
        if payload.get("prediction") is not None:
            state.prediction = payload["prediction"]
        elif force_fresh:
            state.prediction = None

        new_states[p_id] = state
    return new_states


def maybe_reload_manifest(
    current_manifest: dict[str, dict[str, Any]],
    current_states: dict[str, PitcherState],
    last_signature: tuple[int, int],
    last_checked_ts: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, PitcherState], tuple[int, int], float, bool]:
    """
    Hot-reload manifest when file changes.
    Returns (manifest, states, signature, checked_ts, reloaded).
    """
    now_ts = time.time()
    if (now_ts - last_checked_ts) < MANIFEST_RELOAD_MIN_SECONDS:
        return current_manifest, current_states, last_signature, last_checked_ts, False

    if not MANIFEST_PATH.exists():
        logger.warning("Manifest missing during runtime; keeping current in-memory slate.")
        return current_manifest, current_states, last_signature, now_ts, False

    try:
        signature = _manifest_signature(MANIFEST_PATH)
    except FileNotFoundError:
        logger.warning("Manifest disappeared during signature check; keeping existing slate.")
        return current_manifest, current_states, last_signature, now_ts, False

    if signature == last_signature:
        return current_manifest, current_states, signature, now_ts, False

    disk_manifest = _load_manifest_with_retry(MANIFEST_PATH)
    if not disk_manifest:
        logger.warning("Manifest reload returned empty payload; ignoring file update.")
        return current_manifest, current_states, signature, now_ts, False

    new_slate = _is_new_slate(current_manifest, disk_manifest)
    if new_slate:
        old_slate = _manifest_slate_date(current_manifest) or "unknown"
        new_slate_date = _manifest_slate_date(disk_manifest) or "unknown"
        logger.info(
            "New slate detected (%s -> %s). Clearing in-memory pitcher states and trackers.",
            old_slate,
            new_slate_date,
        )
    else:
        added = len(set(disk_manifest) - set(current_manifest))
        removed = len(set(current_manifest) - set(disk_manifest))
        if added or removed:
            logger.info(
                "Manifest updated in-place. Reconciliation applied (added=%d, removed=%d).",
                added,
                removed,
            )

    rebuilt_states = _rebuild_states_for_manifest(
        manifest=disk_manifest,
        old_states=current_states,
        force_fresh=new_slate,
    )
    return disk_manifest, rebuilt_states, signature, now_ts, True


def persist_prediction_updates(
    locked_predictions: dict[str, str],
) -> bool:
    """
    Merge only updated prediction fields back into manifest on Supabase.
    """
    if not locked_predictions:
        return False

    disk_manifest_raw: dict[str, Any] = {}
    loaded = False
    
    try:
        from backend.db import get_db
        db = get_db()
        res = db.table("mlb_model_state").select("state_value").eq("state_key", "manifest").execute()
        if res.data:
            disk_manifest_raw = res.data[0]["state_value"]
            loaded = True
    except Exception as e:
        logger.warning(f"Failed to load manifest from Supabase, falling back to local JSON: {e}")
        if MANIFEST_PATH.exists():
            for _ in range(3):
                try:
                    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
                        payload = json.load(fh)
                    if isinstance(payload, dict):
                        disk_manifest_raw = payload
                        loaded = True
                        break
                except Exception:
                    time.sleep(0.2)
                    
    if not loaded:
        return False

    dirty = False
    for p_id, pred in locked_predictions.items():
        p_key = str(p_id)
        if p_key not in disk_manifest_raw:
            continue
        payload = disk_manifest_raw.get(p_key)
        if not isinstance(payload, dict):
            continue
        if payload.get("prediction") != pred:
            payload["prediction"] = pred
            dirty = True

    if dirty:
        try:
            db.table("mlb_model_state").upsert({
                "state_key": "manifest",
                "state_value": disk_manifest_raw,
                "updated_at": datetime.utcnow().isoformat()
            }, on_conflict="state_key").execute()
        except Exception:
            save_json_file(MANIFEST_PATH, disk_manifest_raw)
    return dirty


def load_model_state() -> DirectionalOutsModels:
    """
    Load state from Supabase instead of local JSON.
    """
    payload = {}
    try:
        from backend.db import get_db
        db = get_db()
        res = db.table("mlb_model_state").select("state_value").eq("state_key", "mlb_fatigue_model").execute()
        if res.data:
            payload = res.data[0]["state_value"]
    except Exception as e:
        logger.warning(f"Failed to load model state from Supabase, falling back: {e}")
        payload = load_json_file(MODEL_STATE_PATH, {})

    if not payload or not isinstance(payload, dict):
        return DirectionalOutsModels()

    if "under_model" in payload or "over_model" in payload:
        under_payload = payload.get("under_model") or {}
        over_payload = payload.get("over_model") or {}
        under_model = UnderProbabilityModel.from_dict(
            under_payload,
            default_weights=DEFAULT_UNDER_MODEL_WEIGHTS,
        )
        over_model = UnderProbabilityModel.from_dict(
            over_payload,
            default_weights=DEFAULT_OVER_MODEL_WEIGHTS,
        )
        return DirectionalOutsModels(under=under_model, over=over_model)

    # Legacy format: treat whole payload as under model and create fresh over model.
    under_model = UnderProbabilityModel.from_dict(
        payload,
        default_weights=DEFAULT_UNDER_MODEL_WEIGHTS,
    )
    over_model = UnderProbabilityModel(
        weights=dict(DEFAULT_OVER_MODEL_WEIGHTS),
        learning_rate=MODEL_LEARNING_RATE,
        l2=MODEL_L2,
        updates=0,
    )
    return DirectionalOutsModels(under=under_model, over=over_model)


def save_model_state(models: DirectionalOutsModels) -> None:
    payload = {
        "schema_version": 3,
        "under_model": models.under.to_dict(),
        "over_model": models.over.to_dict(),
        "combined_updates": models.total_updates,
        "normalization": {
            "method": "zscore",
            "blended_warmup": True,
            "min_samples": FEATURE_STANDARDIZER_MIN_SAMPLES,
            "ramp_samples": FEATURE_STANDARDIZER_RAMP_SAMPLES,
        },
        # Legacy mirrors for compatibility with older reporters.
        "weights": models.under.weights,
        "learning_rate": models.under.learning_rate,
        "l2": models.under.l2,
        "updates": models.under.updates,
    }
    
    try:
        from backend.db import get_db
        db = get_db()
        db.table("mlb_model_state").upsert({
            "state_key": "mlb_fatigue_model",
            "state_value": payload,
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="state_key").execute()
    except Exception as e:
        logger.warning(f"Failed to save model state to Supabase, falling back to local JSON: {e}")
        save_json_file(MODEL_STATE_PATH, payload)


def load_pending_predictions() -> list[dict[str, Any]]:
    data = []
    try:
        from backend.db import get_db
        db = get_db()
        res = db.table("mlb_model_state").select("state_value").eq("state_key", "pending_predictions").execute()
        if res.data:
            data = res.data[0]["state_value"]
    except Exception as e:
        logger.warning(f"Failed to load pending predictions from Supabase, falling back to local JSON: {e}")
        data = load_json_file(PENDING_PREDICTIONS_PATH, [])
        
    if isinstance(data, list):
        return data
    return []


def save_pending_predictions(pending: list[dict[str, Any]]) -> None:
    try:
        from backend.db import get_db
        db = get_db()
        db.table("mlb_model_state").upsert({
            "state_key": "pending_predictions",
            "state_value": pending,
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="state_key").execute()
    except Exception as e:
        logger.warning(f"Failed to save pending predictions to Supabase, falling back to local JSON: {e}")
        save_json_file(PENDING_PREDICTIONS_PATH, pending)


def register_pending_prediction(
    pending: list[dict[str, Any]],
    game_pk: int,
    pitcher_id: int,
    prop_line: float,
    predicted_side: str,
    under_features: dict[str, float],
    over_features: dict[str, float],
    under_proba: float,
    over_proba: float,
) -> None:
    record_id = f"{game_pk}_{pitcher_id}_{int(time.time())}"
    predicted_side = str(predicted_side).upper()
    pred_proba = under_proba if predicted_side == "UNDER" else over_proba
    pending.append(
        {
            "id": record_id,
            "game_pk": int(game_pk),
            "pitcher_id": int(pitcher_id),
            "prop_line": float(prop_line),
            "market": "outs_recorded",
            "predicted_side": predicted_side,
            "under_features": {k: float(v) for k, v in under_features.items()},
            "over_features": {k: float(v) for k, v in over_features.items()},
            "under_proba_before": round(float(under_proba), 4),
            "over_proba_before": round(float(over_proba), 4),
            "predicted_proba_before_update": round(float(pred_proba), 4),
            "edge_gap_before": round(abs(float(under_proba) - float(over_proba)), 4),
            # Legacy fields retained for compatibility.
            "features": {k: float(v) for k, v in (under_features if predicted_side == "UNDER" else over_features).items()},
            "predicted_under": 1 if predicted_side == "UNDER" else 0,
            "created_at": datetime.utcnow().isoformat(),
            "resolved": False,
        }
    )
    
    # Write to unified predictions table (Phase 1/3 integration)
    try:
        from backend.db import get_db
        db = get_db()
        db.table("model_predictions").insert({
            "source": "sports_ml",
            "domain": "sports",
            "event_key": f"mlb_pitcher_outs_{pitcher_id}_{game_pk}",
            "outcome": f"{predicted_side} {prop_line}",
            "prob": round(float(pred_proba), 4),
            "market_prob_at_pick": None, # Will be snapshotted later
            "features_snapshot": {k: float(v) for k, v in (under_features if predicted_side == "UNDER" else over_features).items()},
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to write prediction to model_predictions: {e}")


def settle_pending_predictions(
    pending: list[dict[str, Any]],
    models: DirectionalOutsModels,
    all_outs: dict[int, int],
    game_status_map: dict[int, dict[str, Any]],
) -> int:
    resolved_count = 0
    for rec in pending:
        if rec.get("resolved"):
            continue

        game_pk = int(rec.get("game_pk", 0))
        status = game_status_map.get(game_pk, {})
        if not status.get("is_final"):
            continue

        pitcher_id = int(rec.get("pitcher_id", 0))
        if pitcher_id not in all_outs:
            continue

        final_outs = float(all_outs[pitcher_id])
        prop_line = float(rec.get("prop_line", 0.0))
        under_hit = 1 if final_outs < prop_line else 0
        over_hit = 1 - under_hit

        predicted_side = str(
            rec.get("predicted_side")
            or ("UNDER" if int(rec.get("predicted_under", 1)) == 1 else "OVER")
        ).upper()
        if predicted_side not in {"UNDER", "OVER"}:
            predicted_side = "UNDER"

        under_features = rec.get("under_features")
        over_features = rec.get("over_features")
        has_dual_features = isinstance(under_features, dict) and isinstance(over_features, dict)

        if has_dual_features:
            u_features = {k: float(v) for k, v in under_features.items()}
            o_features = {k: float(v) for k, v in over_features.items()}

            under_before = models.under.predict_proba(u_features)
            over_before = models.over.predict_proba(o_features)

            models.under.update(u_features, under_hit)
            models.over.update(o_features, over_hit)
        else:
            # Legacy pending payload migration path: only under model had features.
            legacy_features = rec.get("features") or {}
            if not isinstance(legacy_features, dict):
                legacy_features = {}
            u_features = {k: float(v) for k, v in legacy_features.items()}
            under_before = models.under.predict_proba(u_features) if u_features else 0.5
            if u_features:
                models.under.update(u_features, under_hit)

            over_before = rec.get("over_proba_before")
            if over_before is None:
                over_before = 1.0 - under_before

        rec["resolved"] = True
        rec["resolved_at"] = datetime.utcnow().isoformat()
        rec["final_outs"] = final_outs
        rec["label_under"] = int(under_hit)
        rec["label_over"] = int(over_hit)
        rec["under_proba_before_update"] = round(float(under_before), 4)
        rec["over_proba_before_update"] = round(float(over_before), 4)
        rec["proba_before_update"] = round(
            float(under_before if predicted_side == "UNDER" else over_before),
            4,
        )
        rec["predicted_side_correct"] = bool(
            (predicted_side == "UNDER" and under_hit == 1)
            or (predicted_side == "OVER" and over_hit == 1)
        )
        resolved_count += 1

    return resolved_count


# ---------------------------------------------------------------------------
# Plausibility + CUSUM evaluation
# ---------------------------------------------------------------------------


def is_pitch_plausible(
    pitch: dict[str, Any],
    baseline: dict[str, Any],
    state: PitcherState,
) -> tuple[bool, str, dict[str, float]]:
    """
    Layered plausibility checks using adaptive z-scores.
    Returns (is_ok, reason, metric_zscores).
    """
    pitch_name = pitch.get("pitch_name")
    if not pitch_name or pitch_name not in baseline:
        return False, "not_in_arsenal", {}

    base = baseline[pitch_name]
    required_fields = ("release_speed", "release_pos_z", "release_pos_x")
    for req in required_fields:
        if pitch.get(req) is None:
            return False, f"missing_{req}", {}

    metric_zscores: dict[str, float] = {}
    for metric in METRICS:
        observed = pitch.get(metric)
        baseline_mean = base.get(metric)
        if observed is None or baseline_mean is None:
            continue

        stat = state.get_stat(pitch_name, metric, base)
        z = stat.zscore(float(observed))
        metric_zscores[metric] = z

        limit = SPEED_Z_OUTLIER_LIMIT if metric == "release_speed" else Z_OUTLIER_LIMIT
        if abs(z) > limit:
            return False, f"{metric}_z_outlier_{z:.2f}", metric_zscores

    # Cross-family sanity check on speed in z-space
    speed = pitch.get("release_speed")
    if speed is not None:
        own_speed_stat = state.get_stat(pitch_name, "release_speed", base)
        own_abs_z = abs(own_speed_stat.zscore(float(speed)))
        own_family = get_pitch_family(pitch_name)

        best_other_name = None
        best_other_abs_z = 999.0
        for other_name, other_base in baseline.items():
            if other_name == pitch_name:
                continue
            if get_pitch_family(other_name) == own_family:
                continue
            if other_base.get("release_speed") is None:
                continue
            other_stat = state.get_stat(other_name, "release_speed", other_base)
            other_abs_z = abs(other_stat.zscore(float(speed)))
            if other_abs_z < best_other_abs_z:
                best_other_abs_z = other_abs_z
                best_other_name = other_name

        if best_other_name and (best_other_abs_z + 1.25 < own_abs_z):
            return False, f"likely_misclassified_as_{best_other_name}", metric_zscores

    return True, "ok", metric_zscores


def evaluate_fatigue_cusum(
    pitch_type: str,
    metric_zscores: dict[str, float],
    state: PitcherState,
    base: dict[str, Any],
) -> tuple[bool, int, dict[str, Any], float]:
    """
    Updates metric CUSUMs and votes on fatigue.
    """
    signals_fired = 0
    signal_details: dict[str, Any] = {}

    for metric in METRICS:
        if metric not in metric_zscores:
            continue

        z_raw = float(metric_zscores[metric])
        fatigue_z = fatigue_z_from_metric(metric, z_raw)
        stat = state.get_stat(pitch_type, metric, base)
        sigma_ratio = clamp(stat.sigma_ratio(), 0.5, 2.0)

        # More volatile pitcher -> looser CUSUM (reduces false positives).
        k_scale = clamp(1.0 + 0.30 * (sigma_ratio - 1.0), 0.85, 1.30)
        h_scale = clamp(1.0 + 0.35 * (sigma_ratio - 1.0), 0.85, 1.35)

        tracker = state.get_tracker(pitch_type, metric)
        result = tracker.update(fatigue_z=fatigue_z, k_scale=k_scale, h_scale=h_scale)

        signal_details[metric] = {
            "z_raw": round(z_raw, 3),
            "fatigue_z": round(fatigue_z, 3),
            **result,
        }
        if result.get("fatigue_signal"):
            signals_fired += 1

    fatigue_confirmed = signals_fired >= VOTE_THRESHOLD
    avg_cusum = state.avg_fatigue_cusum(pitch_type)
    return fatigue_confirmed, signals_fired, signal_details, avg_cusum


def update_adaptive_stats(
    pitch_type: str,
    pitch: dict[str, Any],
    base: dict[str, Any],
    state: PitcherState,
    quality: float,
) -> None:
    for metric in METRICS:
        observed = pitch.get(metric)
        baseline_mean = base.get(metric)
        if observed is None or baseline_mean is None:
            continue
        stat = state.get_stat(pitch_type, metric, base)
        stat.update(float(observed), quality=quality)


def update_recovery_state(
    pitch_quality: float,
    state: PitcherState,
) -> tuple[str, float]:
    """
    Returns (status, decay_applied):
      status in {"inactive", "monitoring", "recovered", "failed"}
    """
    if not state.recovery.active:
        return "inactive", 0.0

    state.recovery.update(pitch_quality)

    volume_ratio = clamp(
        state.recovery.weighted_volume / max(RECOVERY_MIN_EFFECTIVE_VOLUME, 1e-6), 0.0, 1.0
    )
    decay = (
        RECOVERY_BASE_DECAY
        + RECOVERY_QUALITY_DECAY_MULT * pitch_quality
        + RECOVERY_VOLUME_DECAY_MULT * volume_ratio
    )

    for tracker in state.iter_all_trackers():
        tracker.decay(decay)

    avg_cusum = state.avg_fatigue_cusum()
    if (
        state.recovery.pitches_seen >= RECOVERY_MIN_PITCHES
        and state.recovery.weighted_volume >= RECOVERY_MIN_EFFECTIVE_VOLUME
        and state.recovery.posterior_good >= RECOVERY_POSTERIOR_THRESHOLD
        and avg_cusum <= RECOVERY_CUSUM_UNLOCK
    ):
        state.recovery.reset()
        return "recovered", decay

    if (
        state.recovery.pitches_seen >= RECOVERY_MAX_PITCHES
        and (
            state.recovery.posterior_good < RECOVERY_FAIL_POSTERIOR
            or avg_cusum > CUSUM_BASE_H * 0.80
        )
    ):
        state.recovery.active = False
        return "failed", decay

    return "monitoring", decay


# ---------------------------------------------------------------------------
# Managerial hook model
# ---------------------------------------------------------------------------


def should_skip_for_managerial_hook(
    data: dict[str, Any],
    current_outs: int,
    run_diff: int,
    context: dict[str, Any],
) -> tuple[bool, str, float]:
    """
    Returns (skip, reason, under_signal_strength in [0,1]).
    """
    pitch_count = int(context.get("pitch_count", 0))
    inning = int(context.get("inning", 1))
    simplified_li = float(context.get("simplified_li", 0.0))
    tier = int(data.get("tier", 3))
    prop_line = float(data["prop_line"])
    outs_needed = float(prop_line - current_outs)
    bullpen_status = str(data.get("bullpen_status", "average"))
    advanced_context = data.get("advanced_context", {}) if isinstance(data.get("advanced_context", {}), dict) else {}
    manager_hook_score = clamp(safe_float(advanced_context.get("manager_hook_score"), 0.50), 0.0, 1.5)
    ttto_penalty = clamp(safe_float(advanced_context.get("ttto_penalty"), 0.50), 0.0, 1.5)
    days_rest = safe_float(advanced_context.get("days_rest"), 4.0)
    season_max_pitch_count = safe_int(advanced_context.get("season_max_pitch_count"), 0)

    if outs_needed <= 0:
        return True, "already_past_line", 0.0

    ceiling = int(PC_CEILING.get(tier, 90))
    if season_max_pitch_count > 0:
        ceiling = min(ceiling, max(75, season_max_pitch_count))
    under_signal = 0.0
    reason = "ok"

    if pitch_count >= ceiling:
        return False, "pitch_count_at_ceiling", 1.0

    dynamic_under_window = 10 + int(round(4.0 * manager_hook_score))
    if pitch_count >= ceiling - dynamic_under_window and outs_needed > 4.0:
        under_signal = max(under_signal, 0.85)
        reason = "near_ceiling_under_signal"

    if manager_hook_score >= 0.70 and pitch_count >= (ceiling - 14) and outs_needed > 2.5:
        under_signal = max(under_signal, 0.88)
        if reason == "ok":
            reason = "manager_hook_profile"

    if ttto_penalty >= 0.75 and inning >= 5 and pitch_count >= (ceiling - 18):
        under_signal = max(under_signal, 0.82)
        if reason == "ok":
            reason = "ttto_penalty_hook"

    if days_rest < 4.0 and inning >= 5 and pitch_count >= (ceiling - 18):
        under_signal = max(under_signal, 0.80)
        if reason == "ok":
            reason = "short_rest_hook"

    if simplified_li > 6.0 and run_diff < 0:
        under_signal = max(under_signal, 0.75)
        if reason == "ok":
            reason = "high_leverage_losing_hook"

    # Blowout with low pitch count: long leash risk for OVER.
    if run_diff >= 5 and inning <= 6 and pitch_count < (ceiling - 12):
        return False, "blowout_long_leash", under_signal

    # If bullpen is taxed and team is leading, manager often stretches starter.
    if bullpen_fresh_score(bullpen_status) < 0.25 and run_diff > 0 and inning < 6:
        return False, "bullpen_taxed_over_risk", under_signal

    # High prop lines late game can induce manager to let ace clear the line.
    if prop_line >= 18.0 and inning >= 6 and outs_needed <= 3.0 and abs(run_diff) <= 2:
        if reason == "ok":
            reason = "manager_line_chase_over_bias"

    return False, reason, under_signal


# ---------------------------------------------------------------------------
# Context / parsers
# ---------------------------------------------------------------------------


def _parse_context(live_data: dict[str, Any], pitcher_id: int) -> dict[str, Any]:
    pitch_count = 0
    for side in ("home", "away"):
        players = live_data.get("boxscore", {}).get("teams", {}).get(side, {}).get("players", {})
        for p_data in players.values():
            if p_data.get("person", {}).get("id") == pitcher_id:
                pitch_count = p_data.get("stats", {}).get("pitching", {}).get("pitchesThrown", 0)

    linescore = live_data.get("linescore", {})
    inning = int(linescore.get("currentInning", 1) or 1)
    outs_in_inning = int(linescore.get("outs", 0) or 0)

    offense = linescore.get("offense", {}) or {}
    runners_on = sum(
        [
            1 if offense.get("first") else 0,
            1 if offense.get("second") else 0,
            1 if offense.get("third") else 0,
        ]
    )

    base_leverage = (runners_on * 1.5) + (outs_in_inning * 0.5)
    inning_multiplier = 1.0 if inning < 6 else (1.5 if inning < 8 else 2.0)
    simplified_li = base_leverage * inning_multiplier

    return {
        "pitch_count": pitch_count,
        "inning": inning,
        "runners_on": runners_on,
        "simplified_li": float(simplified_li),
    }


def _parse_pitches(
    res: dict[str, Any], game_pk: int
) -> tuple[pd.DataFrame, dict[int, int], dict[int, int], dict[int, dict[str, Any]], dict[str, Any]]:
    pitches: list[dict[str, Any]] = []
    pitcher_outs: dict[int, int] = {}
    pitcher_run_diff: dict[int, int] = {}
    pitcher_context: dict[int, dict[str, Any]] = {}

    live_data = res.get("liveData", {}) or {}
    game_data = res.get("gameData", {}) or {}
    status = game_data.get("status", {}) or {}
    detailed_state = status.get("detailedState", "")
    abstract_state = status.get("abstractGameState", "")
    is_final = abstract_state == "Final" or "Final" in str(detailed_state)
    game_status = {"is_final": bool(is_final), "detailed_state": detailed_state}

    # Scoreboard
    linescore_teams = live_data.get("linescore", {}).get("teams", {}) or {}
    home_runs = int(linescore_teams.get("home", {}).get("runs") or 0)
    away_runs = int(linescore_teams.get("away", {}).get("runs") or 0)

    # Boxscore per-pitcher outs/run_diff
    teams = live_data.get("boxscore", {}).get("teams", {}) or {}
    for side in ("home", "away"):
        players = teams.get(side, {}).get("players", {}) or {}
        for p_data in players.values():
            p_id = p_data.get("person", {}).get("id")
            if p_id is None:
                continue

            pitching = p_data.get("stats", {}).get("pitching", {}) or {}
            pitcher_outs[int(p_id)] = int(pitching.get("outs", 0) or 0)
            pitcher_run_diff[int(p_id)] = (
                (home_runs - away_runs) if side == "home" else (away_runs - home_runs)
            )

    # Pitch-by-pitch physics
    for play in live_data.get("plays", {}).get("allPlays", []) or []:
        pitcher_id = play.get("matchup", {}).get("pitcher", {}).get("id")
        at_bat_number = play.get("about", {}).get("atBatIndex")
        if pitcher_id and int(pitcher_id) not in pitcher_context:
            pitcher_context[int(pitcher_id)] = _parse_context(live_data, int(pitcher_id))

        for event in play.get("playEvents", []) or []:
            if not event.get("isPitch"):
                continue

            pitch_data = event.get("pitchData", {}) or {}
            if not pitch_data.get("startSpeed"):
                continue

            coords = pitch_data.get("coordinates", {}) or {}
            breaks = pitch_data.get("breaks", {}) or {}
            details = event.get("details", {}) or {}
            count = event.get("count", {}) or {}
            pitch_call = str(details.get("description", "")).strip().lower()
            pitch_code = str(details.get("code", "")).strip().upper()

            pitches.append(
                {
                    "pitcher": int(pitcher_id) if pitcher_id is not None else None,
                    "game_pk": int(game_pk),
                    "at_bat_number": at_bat_number,
                    "pitch_number": event.get("pitchNumber"),
                    "play_id": event.get("playId"),
                    "pitch_name": details.get("type", {}).get("description"),
                    "release_speed": pitch_data.get("startSpeed"),
                    "release_pos_z": coords.get("z0"),
                    "release_pos_x": coords.get("x0"),
                    "release_extension": pitch_data.get("extension"),
                    "release_spin_rate": breaks.get("spinRate"),
                    "pitch_call": pitch_call,
                    "pitch_code": pitch_code,
                    "zone": pitch_data.get("zone"),
                    "balls": count.get("balls"),
                    "strikes": count.get("strikes"),
                }
            )

    return pd.DataFrame(pitches), pitcher_outs, pitcher_run_diff, pitcher_context, game_status


# ---------------------------------------------------------------------------
# Async fetchers
# ---------------------------------------------------------------------------


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=7)) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def get_live_game_pks_async(session: aiohttp.ClientSession, date_str: str) -> list[int]:
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}"
    try:
        data = await _fetch_json(session, url)
        if not data.get("dates"):
            return []
        return [int(g["gamePk"]) for g in data["dates"][0].get("games", [])]
    except Exception as exc:
        logger.warning("Failed to fetch schedule for %s: %s", date_str, exc)
        return []


async def fetch_game_async(
    session: aiohttp.ClientSession, game_pk: int
) -> tuple[pd.DataFrame, dict[int, int], dict[int, int], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
    try:
        data = await _fetch_json(session, url)
        df, outs, run_diffs, contexts, game_status = _parse_pitches(data, game_pk)
        return df, outs, run_diffs, contexts, {int(game_pk): game_status}
    except aiohttp.ClientResponseError as exc:
        logger.error("HTTP %s while fetching game_pk=%s", exc.status, game_pk)
    except asyncio.TimeoutError:
        logger.warning("Timeout fetching game_pk=%s", game_pk)
    except Exception as exc:
        logger.exception("Unexpected error fetching game_pk=%s: %s", game_pk, exc)

    return pd.DataFrame(), {}, {}, {}, {}


async def fetch_all_games(
    game_pks: list[int],
) -> tuple[pd.DataFrame, dict[int, int], dict[int, int], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    all_dfs: list[pd.DataFrame] = []
    all_outs: dict[int, int] = {}
    all_run_diffs: dict[int, int] = {}
    all_contexts: dict[int, dict[str, Any]] = {}
    all_status: dict[int, dict[str, Any]] = {}

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_game_async(session, pk) for pk in game_pks]
        results = await asyncio.gather(*tasks)

    for df, outs, run_diffs, contexts, status_map in results:
        if not df.empty:
            all_dfs.append(df)
        all_outs.update(outs)
        all_run_diffs.update(run_diffs)
        all_contexts.update(contexts)
        all_status.update(status_map)

    combined = pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()
    return combined, all_outs, all_run_diffs, all_contexts, all_status


# ---------------------------------------------------------------------------
# Feature engineering + decision policy
# ---------------------------------------------------------------------------


def build_live_pitch_context_features(pitches_df: pd.DataFrame) -> dict[str, float]:
    """
    Derives in-game command/efficiency context from the current game pitch log.
    These features are recomputed each poll and fed into both UNDER and OVER models.
    """
    defaults = {
        "live_strike_rate": 0.62,
        "live_whiff_rate": 0.11,
        "live_pitches_per_pa": 3.9,
        "live_behind_rate": 0.31,
        "live_zone_rate": 0.49,
        "live_chase_rate": 0.30,
    }
    if pitches_df.empty:
        return defaults

    n = max(len(pitches_df), 1)
    calls = (
        pitches_df["pitch_call"].astype(str).str.lower()
        if "pitch_call" in pitches_df.columns
        else pd.Series([""] * n, index=pitches_df.index)
    )
    codes = (
        pitches_df["pitch_code"].astype(str).str.upper()
        if "pitch_code" in pitches_df.columns
        else pd.Series([""] * n, index=pitches_df.index)
    )

    strike_mask = calls.isin(LIVE_STRIKE_CALLS) | codes.isin({"C", "S", "F", "T", "X", "M", "W"})
    whiff_mask = calls.isin(LIVE_WHIFF_CALLS) | codes.isin({"S", "W", "M"})
    swing_mask = calls.str.contains("swinging strike|foul|in play|missed bunt", na=False) | codes.isin(
        {"S", "W", "M", "F", "T", "X"}
    )

    at_bat = (
        pd.to_numeric(pitches_df["at_bat_number"], errors="coerce")
        if "at_bat_number" in pitches_df.columns
        else pd.Series([np.nan] * n, index=pitches_df.index)
    )
    total_pas = int(at_bat.dropna().nunique())
    if total_pas <= 0:
        total_pas = max(int(n / 4), 1)
    live_pitches_per_pa = float(n / total_pas)

    balls = (
        pd.to_numeric(pitches_df["balls"], errors="coerce")
        if "balls" in pitches_df.columns
        else pd.Series([np.nan] * n, index=pitches_df.index)
    )
    strikes = (
        pd.to_numeric(pitches_df["strikes"], errors="coerce")
        if "strikes" in pitches_df.columns
        else pd.Series([np.nan] * n, index=pitches_df.index)
    )
    if balls.notna().any() and strikes.notna().any():
        live_behind_rate = float((balls > strikes).mean())
    else:
        live_behind_rate = defaults["live_behind_rate"]

    zone = (
        pd.to_numeric(pitches_df["zone"], errors="coerce")
        if "zone" in pitches_df.columns
        else pd.Series([np.nan] * n, index=pitches_df.index)
    )
    valid_zone = zone.notna()
    if valid_zone.any():
        in_zone = valid_zone & zone.between(1, 9)
        out_zone = valid_zone & (~zone.between(1, 9))
        live_zone_rate = float(in_zone.mean())
        out_zone_total = max(int(out_zone.sum()), 1)
        live_chase_rate = float((swing_mask & out_zone).sum() / out_zone_total)
    else:
        live_zone_rate = defaults["live_zone_rate"]
        live_chase_rate = defaults["live_chase_rate"]

    return {
        "live_strike_rate": round(float(strike_mask.mean()), 4),
        "live_whiff_rate": round(float(whiff_mask.mean()), 4),
        "live_pitches_per_pa": round(live_pitches_per_pa, 4),
        "live_behind_rate": round(live_behind_rate, 4),
        "live_zone_rate": round(live_zone_rate, 4),
        "live_chase_rate": round(live_chase_rate, 4),
    }


def build_directional_feature_vectors(
    data: dict[str, Any],
    current_outs: int,
    run_diff: int,
    context: dict[str, Any],
    fatigue_votes: int,
    avg_cusum: float,
    metric_zscores: dict[str, float],
    hook_signal: float,
) -> tuple[dict[str, float], dict[str, float]]:
    tier = int(data.get("tier", 3))
    prop_line = float(data["prop_line"])
    outs_needed = prop_line - current_outs
    pitch_count = int(context.get("pitch_count", 0))
    inning = int(context.get("inning", 1))
    simplified_li = float(context.get("simplified_li", 0.0))
    bullpen_status = str(data.get("bullpen_status", "average"))
    ceiling = int(PC_CEILING.get(tier, 90))
    advanced_context = data.get("advanced_context", {}) if isinstance(data.get("advanced_context", {}), dict) else {}
    opponent_context = data.get("opponent_context", {}) if isinstance(data.get("opponent_context", {}), dict) else {}
    environment_context = data.get("environment_context", {}) if isinstance(data.get("environment_context", {}), dict) else {}
    line_movement = data.get("line_movement", {}) if isinstance(data.get("line_movement", {}), dict) else {}
    starter_profile = data.get("starter_profile", {}) if isinstance(data.get("starter_profile", {}), dict) else {}
    bullpen_context = data.get("bullpen_context", {}) if isinstance(data.get("bullpen_context", {}), dict) else {}
    matchup_context = data.get("matchup_context", {}) if isinstance(data.get("matchup_context", {}), dict) else {}

    fatigue_vote_frac = fatigue_votes / max(len(METRICS), 1)
    li_norm = clamp(simplified_li / 8.0, 0.0, 2.0)
    inning_norm = clamp(inning / 9.0, 0.0, 1.5)
    pitch_count_ratio = clamp(pitch_count / max(ceiling, 1), 0.0, 1.6)
    bullpen_fresh = bullpen_fresh_score(bullpen_status)

    manager_hook_score = clamp(safe_float(advanced_context.get("manager_hook_score"), 0.50), 0.0, 1.5)
    ttto_penalty = clamp(safe_float(advanced_context.get("ttto_penalty"), 0.50), 0.0, 1.5)
    days_rest = safe_float(advanced_context.get("days_rest"), 4.0)
    days_rest_short = clamp((4.0 - days_rest) / 4.0, 0.0, 1.2)
    days_rest_fresh = clamp((days_rest - 4.0) / 4.0, 0.0, 1.2)
    rolling_pc3 = safe_float(advanced_context.get("rolling_pitch_count_3"), pitch_count)
    rolling_pc_hot = clamp((rolling_pc3 - (0.78 * ceiling)) / 30.0, 0.0, 1.6)
    velocity_decay_46 = clamp(safe_float(advanced_context.get("velocity_decay_4_6"), 0.0) / 3.0, 0.0, 1.6)
    csw_pct = clamp(safe_float(advanced_context.get("csw_pct"), 28.0) / 100.0, 0.0, 1.0)
    f_strike_pct = clamp(safe_float(advanced_context.get("f_strike_pct"), 58.0) / 100.0, 0.0, 1.0)
    zone_pct = clamp(safe_float(advanced_context.get("zone_pct"), 48.0) / 100.0, 0.0, 1.0)
    swstr_pct = clamp(safe_float(advanced_context.get("swstr_pct"), 12.0) / 100.0, 0.0, 1.0)
    behind_pct_norm = clamp(safe_float(advanced_context.get("behind_pct"), 31.0) / 100.0, 0.0, 1.2)
    o_swing_skill = clamp(safe_float(advanced_context.get("o_swing_pct"), 30.0) / 100.0, 0.0, 1.2)
    pitches_per_ip_stress = clamp((safe_float(advanced_context.get("pitches_per_ip"), 16.0) - 13.5) / 5.0, 0.0, 1.6)
    hr_per_9_norm = clamp(safe_float(advanced_context.get("hr_per_9"), 1.1) / 2.2, 0.0, 1.6)
    babip_norm = clamp((safe_float(advanced_context.get("babip"), 0.300) - 0.250) / 0.120, 0.0, 1.6)
    lob_strength = clamp((safe_float(advanced_context.get("lob_pct"), 72.0) - 55.0) / 30.0, 0.0, 1.6)
    rolling_pc10 = safe_float(advanced_context.get("rolling_pitch_count_10"), rolling_pc3)
    long_leash_history = clamp((rolling_pc10 - (0.74 * ceiling)) / 30.0, 0.0, 1.6)
    command_quality = clamp((0.50 * csw_pct) + (0.30 * f_strike_pct) + (0.20 * zone_pct), 0.0, 1.2)
    command_loss = clamp(1.0 - command_quality, 0.0, 1.2)
    p_pa = safe_float(advanced_context.get("pitches_per_pa"), 3.9)
    in_game_pitch_efficiency_stress = clamp((p_pa - 3.5) / 1.3, 0.0, 1.5)
    in_game_pitch_efficiency_quality = clamp((4.4 - p_pa) / 1.6, 0.0, 1.5)
    live_strike_rate = clamp(safe_float(context.get("live_strike_rate"), 0.62), 0.0, 1.0)
    live_whiff_rate = clamp(safe_float(context.get("live_whiff_rate"), 0.11), 0.0, 0.5)
    live_zone_rate = clamp(safe_float(context.get("live_zone_rate"), 0.49), 0.0, 1.0)
    live_chase_rate = clamp(safe_float(context.get("live_chase_rate"), 0.30), 0.0, 1.0)
    live_behind_rate = clamp(safe_float(context.get("live_behind_rate"), 0.31), 0.0, 1.0)
    live_whiff_scaled = clamp(live_whiff_rate / 0.18, 0.0, 1.4)
    live_chase_scaled = clamp(live_chase_rate / 0.45, 0.0, 1.3)
    live_zone_scaled = clamp(live_zone_rate / 0.60, 0.0, 1.3)
    live_command_quality = clamp(
        (0.30 * live_strike_rate)
        + (0.30 * live_whiff_scaled)
        + (0.20 * live_zone_scaled)
        + (0.20 * live_chase_scaled),
        0.0,
        1.4,
    )
    live_command_loss = clamp(1.0 - live_command_quality, 0.0, 1.4)
    live_p_pa = safe_float(context.get("live_pitches_per_pa"), p_pa)
    live_p_pa_stress = clamp((live_p_pa - 3.45) / 1.25, 0.0, 1.6)
    live_p_pa_quality = clamp((4.35 - live_p_pa) / 1.60, 0.0, 1.6)
    fip_norm = clamp((safe_float(advanced_context.get("fip"), 4.00) - 2.5) / 3.5, 0.0, 1.5)
    barrel_rate_norm = clamp(safe_float(advanced_context.get("barrel_pct"), 7.0) / 16.0, 0.0, 1.5)
    hard_hit_rate_norm = clamp(safe_float(advanced_context.get("hard_hit_pct"), 38.0) / 55.0, 0.0, 1.5)
    gb_rate_norm = clamp(safe_float(advanced_context.get("gb_pct"), 43.0) / 70.0, 0.0, 1.5)

    opp_obp = clamp(safe_float(opponent_context.get("obp"), 0.320), 0.250, 0.420)
    opponent_contact = clamp((0.340 - safe_float(opponent_context.get("k_pct"), 0.22)) / 0.18, 0.0, 1.5)
    opponent_k_rate = clamp(safe_float(opponent_context.get("k_pct"), 0.22) / 0.35, 0.0, 1.5)
    opp_bb_rate = clamp(safe_float(opponent_context.get("bb_pct"), 0.08) / 0.16, 0.0, 1.5)
    opp_work_rate = clamp((safe_float(opponent_context.get("pitches_per_pa"), 3.9) - 3.3) / 1.3, 0.0, 1.5)
    opp_wrc_plus = clamp((safe_float(opponent_context.get("wrc_plus"), 100.0) - 70.0) / 80.0, 0.0, 1.5)
    lineup_balance_risk = clamp(abs(safe_float(opponent_context.get("lineup_handedness_balance"), 0.50) - 0.50) / 0.50, 0.0, 1.2)
    pinch_hit_risk = clamp(safe_float(opponent_context.get("pinch_hit_risk"), 0.10), 0.0, 1.2)

    park_factor = safe_float(environment_context.get("park_factor"), 1.00)
    park_hr_factor = clamp((safe_float(environment_context.get("park_hr_factor"), park_factor) - 0.85) / 0.45, 0.0, 1.6)
    park_pitcher_friendly = clamp((1.15 - park_factor) / 0.35, 0.0, 1.6)
    weather_fatigue = clamp(safe_float(environment_context.get("weather_fatigue_risk"), 0.50), 0.0, 1.5)
    umpire_k_zone = clamp(safe_float(environment_context.get("umpire_k_zone"), 0.50), 0.0, 1.0)
    temperature_stress = clamp((safe_float(environment_context.get("temperature_c"), 22.0) - 22.0) / 15.0, 0.0, 1.5)
    humidity_stress = clamp((safe_float(environment_context.get("humidity_pct"), 55.0) - 55.0) / 35.0, 0.0, 1.5)
    wind_stress = clamp((safe_float(environment_context.get("wind_speed_kph"), 10.0) - 10.0) / 28.0, 0.0, 1.5)
    altitude_stress = clamp((safe_float(environment_context.get("altitude_m"), 100.0) - 250.0) / 1700.0, 0.0, 1.5)

    opening_line = safe_float(line_movement.get("opening_line"), prop_line)
    line_move_delta = safe_float(line_movement.get("line_move_delta"), prop_line - opening_line)
    line_move_delta_norm = clamp(line_move_delta / 2.0, -1.5, 1.5)
    line_move_abs_norm = clamp(abs(line_move_delta) / 2.0, 0.0, 1.5)
    last_move_up_norm = clamp(safe_float(line_movement.get("last_move_delta"), 0.0) / 1.5, -1.5, 1.5)
    book_spread = safe_float(line_movement.get("max_book_line"), prop_line) - safe_float(
        line_movement.get("min_book_line"),
        prop_line,
    )
    market_spread_norm = clamp(book_spread / 1.5, 0.0, 1.5)

    expected_outs_baseline = safe_float(
        advanced_context.get("expected_outs_baseline"),
        safe_float(starter_profile.get("avg_outs_per_start"), 15.0),
    )
    prop_vs_expected_gap = clamp((prop_line - expected_outs_baseline) / 4.5, -1.5, 1.5)
    prop_line_level = clamp((prop_line - 14.0) / 7.0, 0.0, 1.8)
    innings_window = max(10.0 - float(inning), 1.0)
    outs_per_inning_needed_norm = clamp((outs_needed / innings_window) / 2.0, 0.0, 2.0)
    bullpen_fatigue_score = clamp(
        safe_float(bullpen_context.get("bullpen_fatigue_score"), 1.0 - bullpen_fresh),
        0.0,
        1.5,
    )
    home_manager_patience = 1.0 if str(matchup_context.get("home_away", "unknown")).lower() == "home" else 0.0
    early_signal_bonus = clamp((7.0 - inning) / 6.0, 0.0, 1.2)

    under_features = {
        "fatigue_vote_frac": fatigue_votes / max(len(METRICS), 1),
        "avg_cusum_norm": clamp(avg_cusum / max(CUSUM_BASE_H, 1e-6), 0.0, 2.5),
        "pitch_count_ratio": pitch_count_ratio,
        "outs_needed_norm": clamp(outs_needed / 9.0, 0.0, 2.0),
        "vel_drop_z": clamp(max(0.0, -metric_zscores.get("release_speed", 0.0)), 0.0, 4.0),
        "spin_drop_z": clamp(max(0.0, -metric_zscores.get("release_spin_rate", 0.0)), 0.0, 4.0),
        "run_diff_neg": clamp(max(0.0, -run_diff) / 6.0, 0.0, 2.0),
        "li_norm": li_norm,
        "inning_norm": inning_norm,
        "near_ceiling": 1.0 if pitch_count >= (ceiling - 10) else 0.0,
        "bullpen_fresh": bullpen_fresh,
        "hook_signal": clamp(hook_signal, 0.0, 1.0),
        "manager_hook_score": manager_hook_score,
        "ttto_penalty": ttto_penalty,
        "days_rest_short": days_rest_short,
        "rolling_pc_hot": rolling_pc_hot,
        "velocity_decay_46": velocity_decay_46,
        "command_loss": command_loss,
        "opponent_contact": opponent_contact,
        "opp_work_rate": opp_work_rate,
        "park_hr_factor": park_hr_factor,
        "weather_fatigue": weather_fatigue,
        "umpire_k_zone": umpire_k_zone,
        "line_move_delta_norm": line_move_delta_norm,
        "prop_vs_expected_gap": prop_vs_expected_gap,
        "fip_norm": fip_norm,
        "barrel_rate_norm": barrel_rate_norm,
        "hard_hit_rate_norm": hard_hit_rate_norm,
        "gb_rate_norm": gb_rate_norm,
        "in_game_pitch_efficiency_stress": in_game_pitch_efficiency_stress,
        "live_command_loss": live_command_loss,
        "live_p_pa_stress": live_p_pa_stress,
        "live_behind_rate": live_behind_rate,
        "opp_bb_rate": opp_bb_rate,
        "lineup_balance_risk": lineup_balance_risk,
        "pinch_hit_risk": pinch_hit_risk,
        "bullpen_fatigue_score": bullpen_fatigue_score,
        "temperature_stress": temperature_stress,
        "humidity_stress": humidity_stress,
        "wind_stress": wind_stress,
        "altitude_stress": altitude_stress,
        "line_move_abs_norm": line_move_abs_norm,
        "market_spread_norm": market_spread_norm,
        "last_move_up_norm": last_move_up_norm,
        "prop_line_level": prop_line_level,
        "outs_per_inning_needed_norm": outs_per_inning_needed_norm,
        "o_swing_skill": o_swing_skill,
        "behind_pct_norm": behind_pct_norm,
        "pitches_per_ip_stress": pitches_per_ip_stress,
        "hr_per_9_norm": hr_per_9_norm,
        "babip_norm": babip_norm,
        "lob_strength": lob_strength,
        "long_leash_history": long_leash_history,
        "home_manager_patience": home_manager_patience,
        "early_signal_bonus": early_signal_bonus,
        # Optional backup context features for future weights.
        "opp_obp_norm": clamp((opp_obp - 0.280) / 0.120, 0.0, 1.5),
        "opp_wrc_plus_norm": opp_wrc_plus,
    }
    over_features = {
        "stability_score": clamp(1.0 - fatigue_vote_frac, 0.0, 1.0),
        "rebound_signal_norm": clamp(1.0 - under_features["avg_cusum_norm"], 0.0, 1.5),
        "pitch_count_buffer": clamp(1.0 - pitch_count_ratio, 0.0, 1.2),
        "outs_needed_short_norm": clamp(1.0 - (outs_needed / 9.0), 0.0, 1.5),
        "run_diff_pos": clamp(max(0.0, run_diff) / 6.0, 0.0, 2.0),
        "li_low": clamp(1.0 - li_norm, 0.0, 1.5),
        "inning_early": clamp(1.0 - inning_norm, 0.0, 1.2),
        "bullpen_taxed": clamp(1.0 - bullpen_fresh, 0.0, 1.0),
        "hook_risk_low": clamp(1.0 - clamp(hook_signal, 0.0, 1.0), 0.0, 1.0),
        "vel_stability": clamp(math.exp(-abs(metric_zscores.get("release_speed", 0.0))), 0.0, 1.0),
        "spin_stability": clamp(math.exp(-abs(metric_zscores.get("release_spin_rate", 0.0))), 0.0, 1.0),
        "manager_hook_score": manager_hook_score,
        "ttto_penalty": ttto_penalty,
        "days_rest_fresh": days_rest_fresh,
        "rolling_pc_hot": rolling_pc_hot,
        "velocity_decay_46": velocity_decay_46,
        "command_quality": command_quality + (0.10 * swstr_pct),
        "opponent_k_rate": opponent_k_rate,
        "opp_work_rate": opp_work_rate,
        "park_pitcher_friendly": park_pitcher_friendly,
        "weather_fatigue": weather_fatigue,
        "umpire_k_zone": umpire_k_zone,
        "line_move_delta_norm": line_move_delta_norm,
        "prop_vs_expected_gap": prop_vs_expected_gap,
        "fip_norm": fip_norm,
        "barrel_rate_norm": barrel_rate_norm,
        "hard_hit_rate_norm": hard_hit_rate_norm,
        "gb_rate_norm": gb_rate_norm,
        "in_game_pitch_efficiency_quality": in_game_pitch_efficiency_quality,
        "live_command_quality": live_command_quality,
        "live_p_pa_quality": live_p_pa_quality,
        "live_behind_rate": live_behind_rate,
        "opp_bb_rate": opp_bb_rate,
        "lineup_balance_risk": lineup_balance_risk,
        "pinch_hit_risk": pinch_hit_risk,
        "bullpen_fatigue_score": bullpen_fatigue_score,
        "temperature_stress": temperature_stress,
        "humidity_stress": humidity_stress,
        "wind_stress": wind_stress,
        "altitude_stress": altitude_stress,
        "line_move_abs_norm": line_move_abs_norm,
        "market_spread_norm": market_spread_norm,
        "last_move_up_norm": last_move_up_norm,
        "prop_line_level": prop_line_level,
        "outs_per_inning_needed_norm": outs_per_inning_needed_norm,
        "o_swing_skill": o_swing_skill,
        "behind_pct_norm": behind_pct_norm,
        "pitches_per_ip_stress": pitches_per_ip_stress,
        "hr_per_9_norm": hr_per_9_norm,
        "babip_norm": babip_norm,
        "lob_strength": lob_strength,
        "long_leash_history": long_leash_history,
        "home_manager_patience": home_manager_patience,
        "early_signal_bonus": early_signal_bonus,
        # Optional backup context features for future weights.
        "opp_wrc_plus_norm": opp_wrc_plus,
    }
    return under_features, over_features


def choose_outs_side(
    under_proba: float,
    over_proba: float,
    outs_needed: float,
    fatigue_confirmed: bool,
    avg_cusum: float,
    hook_reason: str,
    recovery_status: str,
    pitch_count: int,
    ceiling: int,
    total_model_updates: int,
    no_bet_profile: dict[str, float],
) -> tuple[Optional[str], str]:
    if outs_needed <= 0:
        return None, "already_past_line"

    under_min_outs_needed = float(no_bet_profile["under_min_outs_needed"])
    over_max_outs_needed = float(no_bet_profile["over_max_outs_needed"])
    over_pitch_count_buffer = float(no_bet_profile["over_pitch_count_buffer"])
    under_model_only_edge_bonus = float(no_bet_profile["under_model_only_edge_bonus"])
    maturity_updates = int(no_bet_profile["model_maturity_updates"])

    mature = total_model_updates >= maturity_updates
    min_side_proba = float(no_bet_profile["side_proba_mature"]) if mature else float(no_bet_profile["side_proba_early"])
    min_edge_gap = float(no_bet_profile["edge_gap_mature"]) if mature else float(no_bet_profile["edge_gap_early"])

    edge_gap = abs(under_proba - over_proba)
    if max(under_proba, over_proba) < min_side_proba:
        return None, "no_bet_low_confidence"
    if edge_gap < min_edge_gap:
        return None, "no_bet_small_edge"

    side = "UNDER" if under_proba > over_proba else "OVER"

    if side == "UNDER":
        if outs_needed <= under_min_outs_needed:
            return None, "no_bet_under_too_close"
        if hook_reason in {"blowout_long_leash", "bullpen_taxed_over_risk"}:
            return None, "no_bet_under_context_conflict"
        if hook_reason in {"pitch_count_at_ceiling", "near_ceiling_under_signal", "high_leverage_losing_hook"}:
            return side, f"managerial_{hook_reason}"
        if recovery_status == "failed":
            return side, "fatigue_persisted"
        if fatigue_confirmed and avg_cusum >= (0.70 * CUSUM_BASE_H):
            return side, "cusum_fatigue"
        if under_proba >= (min_side_proba + under_model_only_edge_bonus):
            return side, "model_only_under_edge"
        return None, "no_bet_under_not_confirmed"

    # OVER side filters.
    if outs_needed > over_max_outs_needed:
        return None, "no_bet_over_too_far"
    if pitch_count >= (ceiling - over_pitch_count_buffer):
        return None, "no_bet_over_pitch_count_risk"
    if hook_reason in {"pitch_count_at_ceiling", "near_ceiling_under_signal", "high_leverage_losing_hook"}:
        return None, "no_bet_over_hook_risk"
    if fatigue_confirmed and avg_cusum >= (0.80 * CUSUM_BASE_H):
        return None, "no_bet_over_fatigue_signal"
    if recovery_status == "failed":
        return None, "no_bet_over_recovery_failed"
    return side, "stability_over_edge"


# ---------------------------------------------------------------------------
# Alert reason message
# ---------------------------------------------------------------------------


def build_reason_message(
    lock_side: str,
    lock_trigger: str,
    under_proba: float,
    over_proba: float,
    current_outs: int,
    prop_line: float,
    run_diff: int,
    tier: int,
    bp_status: str,
    pitch_count: int,
    inning: int,
    simplified_li: float,
    avg_cusum: float,
    fatigue_votes: int,
    signal_details: dict[str, Any],
    recovery_state: BayesianRecoveryState,
    data: Optional[dict[str, Any]] = None,
    live_context: Optional[dict[str, Any]] = None,
) -> str:
    lines = [
        f"Lock Side         : {lock_side}",
        f"Lock Trigger      : {lock_trigger}",
        f"Model P(UNDER)    : {under_proba:.3f}",
        f"Model P(OVER)     : {over_proba:.3f}",
        f"Model Edge Gap    : {abs(under_proba - over_proba):.3f}",
        f"CUSUM Avg (fatigue): {avg_cusum:.2f} | Votes: {fatigue_votes}/{len(METRICS)}",
    ]

    for metric, detail in signal_details.items():
        if detail.get("fatigue_signal"):
            lines.append(
                f"  {metric}: z={detail.get('z_raw')} fatigue_z={detail.get('fatigue_z')} "
                f"CUSUM={detail.get('cusum_fatigue')}"
            )

    lines += [
        f"Recovery Posterior: {recovery_state.posterior_good:.3f} "
        f"(n={recovery_state.effective_n:.1f}, volume={recovery_state.weighted_volume:.2f})",
        f"Outs              : {current_outs} / {prop_line}",
        f"Pitch Count       : {pitch_count} | Inning: {inning}",
        f"Run Diff          : {run_diff:+d} | LI: {simplified_li:.2f}",
        f"Tier              : {tier} | Bullpen: {bp_status}",
    ]

    if isinstance(data, dict):
        line_ctx = data.get("line_movement", {}) if isinstance(data.get("line_movement", {}), dict) else {}
        adv_ctx = data.get("advanced_context", {}) if isinstance(data.get("advanced_context", {}), dict) else {}
        opp_ctx = data.get("opponent_context", {}) if isinstance(data.get("opponent_context", {}), dict) else {}
        env_ctx = data.get("environment_context", {}) if isinstance(data.get("environment_context", {}), dict) else {}

        if line_ctx:
            open_line = safe_float(line_ctx.get("opening_line"), prop_line)
            current_line = safe_float(line_ctx.get("current_line"), prop_line)
            move_delta = safe_float(line_ctx.get("line_move_delta"), 0.0)
            last_move_delta = safe_float(line_ctx.get("last_move_delta"), 0.0)
            book_spread = safe_float(line_ctx.get("max_book_line"), current_line) - safe_float(
                line_ctx.get("min_book_line"),
                current_line,
            )
            lines.append(
                f"Line Move         : {open_line:.1f} -> {current_line:.1f} "
                f"(open delta {move_delta:+.1f}, last move {last_move_delta:+.1f})"
            )
            lines.append(
                f"Market Consensus  : spread {book_spread:.1f} across "
                f"{safe_int(line_ctx.get('book_count'), 0)} books"
            )

        if adv_ctx:
            lines.append(
                "Pitching Profile  : "
                f"CSW {safe_float(adv_ctx.get('csw_pct'), 0.0):.1f}% | "
                f"SwStr {safe_float(adv_ctx.get('swstr_pct'), 0.0):.1f}% | "
                f"P/PA {safe_float(adv_ctx.get('pitches_per_pa'), 0.0):.2f} | "
                f"DaysRest {safe_float(adv_ctx.get('days_rest'), 0.0):.1f}"
            )
            lines.append(
                "Workload/Hook     : "
                f"RollPC3 {safe_float(adv_ctx.get('rolling_pitch_count_3'), 0.0):.1f} | "
                f"SeasonMax {safe_float(adv_ctx.get('season_max_pitch_count'), 0.0):.0f} | "
                f"HookScore {safe_float(adv_ctx.get('manager_hook_score'), 0.0):.2f} | "
                f"TTTO {safe_float(adv_ctx.get('ttto_penalty'), 0.0):.2f}"
            )

        if opp_ctx:
            lines.append(
                "Opponent Context  : "
                f"{opp_ctx.get('team', '?')} | "
                f"OBP {safe_float(opp_ctx.get('obp'), 0.0):.3f} | "
                f"K% {safe_float(opp_ctx.get('k_pct'), 0.0) * 100:.1f}% | "
                f"wRC+ {safe_float(opp_ctx.get('wrc_plus'), 0.0):.0f}"
            )

        if env_ctx:
            lines.append(
                "Environment       : "
                f"Park {safe_float(env_ctx.get('park_factor'), 1.0):.2f} | "
                f"HR Park {safe_float(env_ctx.get('park_hr_factor'), 1.0):.2f} | "
                f"Ump KZone {safe_float(env_ctx.get('umpire_k_zone'), 0.5):.2f} | "
                f"WeatherRisk {safe_float(env_ctx.get('weather_fatigue_risk'), 0.5):.2f}"
            )

    if isinstance(live_context, dict):
        lines.append(
            "Live Command      : "
            f"Strike {100.0 * safe_float(live_context.get('live_strike_rate'), 0.0):.1f}% | "
            f"Whiff {100.0 * safe_float(live_context.get('live_whiff_rate'), 0.0):.1f}% | "
            f"P/PA {safe_float(live_context.get('live_pitches_per_pa'), 0.0):.2f} | "
            f"Behind {100.0 * safe_float(live_context.get('live_behind_rate'), 0.0):.1f}%"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------


def engine(mode: Optional[str] = None) -> None:
    eastern = pytz.timezone("US/Eastern")
    run_mode = normalize_run_mode(mode if mode is not None else os.getenv("OUTS_RUN_MODE", DEFAULT_RUN_MODE))
    no_bet_profile = get_no_bet_profile(run_mode)

    if run_mode == "training":
        logger.warning(
            "RUN_MODE=training active. No-bet filters are intentionally loose for paper learning."
        )
    else:
        logger.info("RUN_MODE=live active. Production no-bet filters enabled.")

    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"{MANIFEST_PATH} not found. Add your monitored pitchers manifest before starting engine."
        )

    manifest: dict[str, dict[str, Any]] = _load_manifest_with_retry(MANIFEST_PATH)
    if not manifest:
        raise ValueError("Manifest is empty or invalid. Orchestrator must populate baseline data first.")
    states: dict[str, PitcherState] = _rebuild_states_for_manifest(
        manifest=manifest,
        old_states={},
        force_fresh=True,
    )

    manifest_signature = _manifest_signature(MANIFEST_PATH)
    manifest_last_checked_ts = 0.0

    model = load_model_state()
    pending_predictions = load_pending_predictions()

    logger.info(
        (
            "Advanced Bullpen Bot v4 Online - mode=%s | tracking=%d | slate=%s | "
            "updates(u/o/t)=%d/%d/%d | pending_labels=%d | "
            "thresholds early(p>=%.2f gap>=%.2f) mature(p>=%.2f gap>=%.2f)"
        ),
        run_mode,
        len(manifest),
        _manifest_slate_date(manifest) or "unknown",
        model.under.updates,
        model.over.updates,
        model.total_updates,
        len([p for p in pending_predictions if not p.get("resolved")]),
        float(no_bet_profile["side_proba_early"]),
        float(no_bet_profile["edge_gap_early"]),
        float(no_bet_profile["side_proba_mature"]),
        float(no_bet_profile["edge_gap_mature"]),
    )

    while True:
        try:
            (
                manifest,
                states,
                manifest_signature,
                manifest_last_checked_ts,
                _reloaded,
            ) = maybe_reload_manifest(
                current_manifest=manifest,
                current_states=states,
                last_signature=manifest_signature,
                last_checked_ts=manifest_last_checked_ts,
            )

            today_str = datetime.now(eastern).strftime("%Y-%m-%d")
            unresolved_game_pks = sorted(
                {
                    int(rec.get("game_pk", 0))
                    for rec in pending_predictions
                    if not rec.get("resolved") and int(rec.get("game_pk", 0)) > 0
                }
            )

            async def _run_fetch():
                async with aiohttp.ClientSession() as session:
                    today_pks = await get_live_game_pks_async(session, today_str)
                all_pks = sorted(set(today_pks) | set(unresolved_game_pks))
                if not all_pks:
                    return pd.DataFrame(), {}, {}, {}, {}
                return await fetch_all_games(all_pks)

            live_data, all_outs, all_run_diffs, all_contexts, game_status_map = asyncio.run(_run_fetch())

            # Resolve any completed game outcomes to self-train model.
            resolved = settle_pending_predictions(
                pending=pending_predictions,
                models=model,
                all_outs=all_outs,
                game_status_map=game_status_map,
            )
            if resolved > 0:
                save_model_state(model)
                save_pending_predictions(pending_predictions)
                logger.info(
                    "Settled %d labels. Model updates now under=%d over=%d total=%d.",
                    resolved,
                    model.under.updates,
                    model.over.updates,
                    model.total_updates,
                )

            if live_data.empty or "pitcher" not in live_data.columns:
                logger.info("Waiting for live pitches...")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            logger.info(
                "Scanned %d live pitches across %d games.",
                len(live_data),
                int(live_data["game_pk"].nunique()),
            )

            pending_dirty = False
            locked_predictions: dict[str, str] = {}

            for p_id, data in manifest.items():
                state = states.get(p_id)
                if state is None:
                    state = PitcherState(p_id=p_id)
                    states[p_id] = state
                if state.prediction is not None:
                    continue

                p_int = int(p_id)
                current_outs = int(all_outs.get(p_int, 0))
                run_diff = int(all_run_diffs.get(p_int, 0))
                context = all_contexts.get(p_int, {})
                if not context:
                    continue

                prop_line = float(data["prop_line"])
                outs_needed = prop_line - current_outs
                tier = int(data.get("tier", 3))
                bp_status = str(data.get("bullpen_status", "Fresh"))
                pitch_count = int(context.get("pitch_count", 0))
                inning = int(context.get("inning", 1))
                simplified_li = float(context.get("simplified_li", 0.0))

                skip, hook_reason, hook_signal = should_skip_for_managerial_hook(
                    data=data,
                    current_outs=current_outs,
                    run_diff=run_diff,
                    context=context,
                )
                if skip:
                    logger.debug("[%s] Skip by hook filter: %s", p_id, hook_reason)
                    continue

                p_pitches = (
                    live_data[live_data["pitcher"] == p_int]
                    .sort_values(["at_bat_number", "pitch_number"])
                    .copy()
                )
                if p_pitches.empty:
                    continue

                total_pitches = len(p_pitches)
                if total_pitches < MIN_PITCHES_BEFORE_TRACKING:
                    continue

                if current_outs < MIN_OUTS_BEFORE_EVALUATION:
                    continue

                baseline = data.get("baseline", {})
                if not baseline:
                    logger.warning("[%s] Missing baseline in manifest. Skipping pitcher.", p_id)
                    continue

                for row_idx, (_, pitch) in enumerate(p_pitches.iterrows()):
                    pitch_dict = pitch.to_dict()
                    p_uid = str(
                        pitch_dict.get("play_id")
                        or f"{pitch_dict.get('game_pk')}_{pitch_dict.get('at_bat_number')}_{pitch_dict.get('pitch_number')}"
                    )
                    if p_uid in state.processed_pitch_ids:
                        continue
                    state.processed_pitch_ids.add(p_uid)

                    pitch_type = pitch_dict.get("pitch_name")
                    if pitch_type not in baseline:
                        continue
                    base = baseline[pitch_type]

                    plausible, plausibility_reason, metric_zscores = is_pitch_plausible(
                        pitch=pitch_dict,
                        baseline=baseline,
                        state=state,
                    )
                    if not plausible:
                        logger.debug(
                            "[%s] Pitch rejected (%s) type=%s",
                            p_id,
                            plausibility_reason,
                            pitch_type,
                        )
                        continue

                    fatigue_confirmed, fatigue_votes, signal_details, avg_cusum = evaluate_fatigue_cusum(
                        pitch_type=pitch_type,
                        metric_zscores=metric_zscores,
                        state=state,
                        base=base,
                    )
                    state.last_signal_details = signal_details

                    pitch_quality = pitch_quality_from_zscores(metric_zscores)

                    # Enter Bayesian recovery monitoring on first fatigue flag.
                    if fatigue_confirmed and not state.recovery.active:
                        state.recovery.activate()
                        logger.debug("[%s] Recovery monitor activated after fatigue signal.", p_id)

                    recovery_status, _decay = update_recovery_state(
                        pitch_quality=pitch_quality,
                        state=state,
                    )
                    if recovery_status == "recovered":
                        logger.info("[%s] Recovery confirmed (posterior %.3f).", p_id, state.recovery.posterior_good)
                        continue

                    # Only adapt baseline on good-quality pitches and when not in confirmed fatigue regime.
                    if (not fatigue_confirmed and pitch_quality >= ADAPTIVE_UPDATE_MIN_QUALITY) or pitch_quality >= 0.90:
                        update_adaptive_stats(
                            pitch_type=pitch_type,
                            pitch=pitch_dict,
                            base=base,
                            state=state,
                            quality=max(0.4, pitch_quality),
                        )

                    live_context = dict(context)
                    live_context.update(build_live_pitch_context_features(p_pitches.iloc[: row_idx + 1]))

                    under_features, over_features = build_directional_feature_vectors(
                        data=data,
                        current_outs=current_outs,
                        run_diff=run_diff,
                        context=live_context,
                        fatigue_votes=fatigue_votes,
                        avg_cusum=avg_cusum,
                        metric_zscores=metric_zscores,
                        hook_signal=hook_signal,
                    )
                    under_proba = model.under.predict_proba(under_features)
                    over_proba = model.over.predict_proba(over_features)
                    state.last_under_proba = under_proba
                    state.last_over_proba = over_proba
                    state.last_under_features = under_features
                    state.last_over_features = over_features

                    ceiling = int(PC_CEILING.get(tier, 90))
                    chosen_side, lock_trigger = choose_outs_side(
                        under_proba=under_proba,
                        over_proba=over_proba,
                        outs_needed=outs_needed,
                        fatigue_confirmed=fatigue_confirmed,
                        avg_cusum=avg_cusum,
                        hook_reason=hook_reason,
                        recovery_status=recovery_status,
                        pitch_count=pitch_count,
                        ceiling=ceiling,
                        total_model_updates=model.total_updates,
                        no_bet_profile=no_bet_profile,
                    )
                    if not chosen_side:
                        continue

                    state.prediction = chosen_side
                    data["prediction"] = chosen_side
                    locked_predictions[p_id] = chosen_side

                    game_pk = int(pitch_dict.get("game_pk", 0) or 0)
                    if game_pk > 0:
                        register_pending_prediction(
                            pending=pending_predictions,
                            game_pk=game_pk,
                            pitcher_id=p_int,
                            prop_line=prop_line,
                            predicted_side=chosen_side,
                            under_features=under_features,
                            over_features=over_features,
                            under_proba=under_proba,
                            over_proba=over_proba,
                        )
                        pending_dirty = True

                    reason_msg = build_reason_message(
                        lock_side=chosen_side,
                        lock_trigger=lock_trigger,
                        under_proba=under_proba,
                        over_proba=over_proba,
                        current_outs=current_outs,
                        prop_line=prop_line,
                        run_diff=run_diff,
                        tier=tier,
                        bp_status=bp_status,
                        pitch_count=pitch_count,
                        inning=inning,
                        simplified_li=simplified_li,
                        avg_cusum=avg_cusum,
                        fatigue_votes=fatigue_votes,
                        signal_details=signal_details,
                        recovery_state=state.recovery,
                        data=data,
                        live_context=live_context,
                    )
                    send_lock_in_ping(
                        name=data["name"],
                        prop="Outs Recorded",
                        line=data["prop_line"],
                        reason=reason_msg,
                        side=chosen_side,
                        under_proba=under_proba,
                        over_proba=over_proba,
                        model_updates_under=model.under.updates,
                        model_updates_over=model.over.updates,
                        mode=run_mode,
                    )
                    logger.info(
                        "[%s] %s locked | trigger=%s | p_u=%.3f | p_o=%.3f | outs=%d/%.1f | pc=%d",
                        data["name"],
                        chosen_side,
                        lock_trigger,
                        under_proba,
                        over_proba,
                        current_outs,
                        prop_line,
                        pitch_count,
                    )
                    break

            if locked_predictions:
                updated = persist_prediction_updates(locked_predictions)
                if updated:
                    logger.info(
                        "Manifest predictions merged for %d new lock(s).",
                        len(locked_predictions),
                    )
                else:
                    logger.warning(
                        "No manifest predictions were persisted for %d lock(s).",
                        len(locked_predictions),
                    )

            if pending_dirty:
                save_pending_predictions(pending_predictions)
                logger.info("Pending predictions saved (%d total).", len(pending_predictions))

        except KeyboardInterrupt:
            logger.info("Engine stopped by user.")
            break
        except Exception as exc:
            logger.exception("Unhandled error in main loop: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    engine()
