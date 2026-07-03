"""
calibrate.py – Model calibration report for pavlov-weather-bot.

Loads all resolved positions from logs/positions.json and checks whether
the model's predicted probabilities match observed win rates.

A well-calibrated model produces:
  - ~70% win rate on trades where model_prob was 0.60-0.80
  - ~90% win rate on trades where model_prob was 0.80-1.00
  - etc.

Usage:
    python calibrate.py              # full report
    python calibrate.py --city NYC   # filter to one city
    python calibrate.py --min 30     # require 30+ trades to show a bucket

Output format:
    Bucket        Trades  W    L    Actual%   Expected%   Diff
    0.55-0.65       12    8    4      67%        60%       +7%
    0.65-0.75        9    7    2      78%        70%       +8%
    ...

The "Diff" column shows whether the model is over- or under-confident in
each probability range.  Ideal: Diff ≈ 0 in every bucket.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

import data_paths as dp
from pipeline import signal_learning_log

_POSITIONS_FILE = os.path.join(dp.logs_dir(), "positions.json")
_SIGNALS_FILE   = os.path.join(dp.logs_dir(), "signals.json")
_BIAS_FILE      = os.path.join(dp.logs_dir(), "ensemble_bias.json")
_ERRORS_FILE    = os.path.join(dp.logs_dir(), "forecast_errors.json")

# Calibration buckets: (lower_inclusive, upper_exclusive, label)
_BUCKETS = [
    (0.50, 0.60, "0.50-0.60"),
    (0.60, 0.70, "0.60-0.70"),
    (0.70, 0.80, "0.70-0.80"),
    (0.80, 0.90, "0.80-0.90"),
    (0.90, 1.01, "0.90-1.00"),
]


def _load_positions() -> list[dict]:
    try:
        with open(_POSITIONS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _bucket_label(model_prob: float) -> str | None:
    for lo, hi, label in _BUCKETS:
        if lo <= model_prob < hi:
            return label
    return None


def run_report(city_filter: str | None, min_trades: int) -> None:
    positions = _load_positions()
    resolved  = [
        p for p in positions
        if p.get("status") in ("won", "lost") and p.get("model_prob") is not None
    ]

    if city_filter:
        resolved = [p for p in resolved if city_filter.lower() in p.get("city", "").lower()]

    if not resolved:
        print("No resolved positions found.")
        if city_filter:
            print(f"(filter: city={city_filter!r})")
        print("\nThe calibration report will populate as markets settle.")
        return

    # ── Per-bucket stats ──────────────────────────────────────────────────
    buckets: dict[str, dict] = {
        label: {"trades": 0, "wins": 0, "losses": 0, "sum_prob": 0.0}
        for _, _, label in _BUCKETS
    }

    skipped = 0
    for p in resolved:
        prob  = float(p["model_prob"])
        # model_prob is for the YES side; adjust for NO bets
        side  = p.get("recommended_side", "yes")
        if side == "no":
            prob = 1.0 - prob

        label = _bucket_label(prob)
        if label is None:
            skipped += 1
            continue

        won = p.get("status") == "won"
        buckets[label]["trades"]   += 1
        buckets[label]["wins"]     += int(won)
        buckets[label]["losses"]   += int(not won)
        buckets[label]["sum_prob"] += prob

    # ── Print report ──────────────────────────────────────────────────────
    title = "Calibration Report"
    if city_filter:
        title += f"  (city filter: {city_filter})"
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"  Total resolved trades: {len(resolved)}  |  Skipped (prob<0.5): {skipped}")
    print(f"{'=' * 60}")
    print(f"  {'Bucket':<12}  {'Trades':>6}  {'W':>4}  {'L':>4}  "
          f"{'Actual%':>8}  {'Expect%':>8}  {'Diff':>7}")
    print(f"  {'-' * 58}")

    overall_wins = overall_trades = 0
    has_data = False

    for _, _, label in _BUCKETS:
        b = buckets[label]
        n = b["trades"]
        if n == 0:
            continue
        if n < min_trades:
            print(f"  {label:<12}  {n:>6}  {'—':>4}  {'—':>4}  "
                  f"  (too few trades to report)")
            continue
        has_data = True
        actual_pct  = b["wins"] / n * 100
        expected_pct = b["sum_prob"] / n * 100
        diff        = actual_pct - expected_pct
        diff_str    = f"{diff:+.1f}%"
        overall_wins   += b["wins"]
        overall_trades += n
        print(f"  {label:<12}  {n:>6}  {b['wins']:>4}  {b['losses']:>4}  "
              f"{actual_pct:>7.1f}%  {expected_pct:>7.1f}%  {diff_str:>7}")

    if not has_data:
        print(f"  (no buckets have >= {min_trades} trades yet)")

    if overall_trades > 0:
        print(f"\n  Overall win rate: {overall_wins}/{overall_trades} "
              f"= {overall_wins / overall_trades * 100:.1f}%")

        # Brier score = mean( (predicted_prob − outcome)^2 )
        # 0.00 = perfect, 0.25 = random/coin-flip, 0.50 = anti-correlated.
        brier_total = 0.0
        for p in resolved:
            prob = float(p["model_prob"])
            if p.get("recommended_side") == "no":
                prob = 1.0 - prob
            outcome = 1.0 if p.get("status") == "won" else 0.0
            brier_total += (prob - outcome) ** 2
        brier = brier_total / len(resolved)
        print(f"  Brier score:      {brier:.4f}  "
              f"({'good' if brier < 0.20 else 'okay' if brier < 0.25 else 'poor'} — "
              f"0.25 = random)")

    # ── Per-city breakdown ────────────────────────────────────────────────
    print(f"\n{'─' * 60}")
    print("  P&L and win rate by city")
    print(f"  {'City':<20}  {'Trades':>6}  {'Wins':>5}  {'Win%':>6}  {'P&L':>8}")
    print(f"  {'-' * 50}")

    by_city: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pl": 0.0})
    for p in resolved:
        city = p.get("city", "Unknown")
        by_city[city]["n"]    += 1
        by_city[city]["wins"] += int(p.get("status") == "won")
        by_city[city]["pl"]   += float(p.get("pl") or 0)

    for city, s in sorted(by_city.items(), key=lambda x: -x[1]["n"]):
        wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
        print(f"  {city:<20}  {s['n']:>6}  {s['wins']:>5}  {wr:>5.1f}%  "
              f"${s['pl']:>+7.2f}")

    # ── Bias table ────────────────────────────────────────────────────────
    try:
        with open(_BIAS_FILE, "r", encoding="utf-8") as fh:
            bias = json.load(fh)
        non_zero = {k: v for k, v in bias.items() if abs(v) > 0.05}
        if non_zero:
            print(f"\n{'─' * 60}")
            print("  Ensemble bias corrections (non-zero cities, °F)")
            print(f"  {'City':<20}  {'Bias':>8}  (positive = model ran too warm)")
            print(f"  {'-' * 40}")
            for city, b in sorted(non_zero.items(), key=lambda x: abs(x[1]), reverse=True):
                direction = "too warm" if b > 0 else "too cold"
                print(f"  {city:<20}  {b:>+7.2f}°F  ({direction})")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # ── Forecast errors: source skill + adaptive sigma ────────────────────
    try:
        with open(_ERRORS_FILE, "r", encoding="utf-8") as fh:
            errors = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        errors = []

    if errors:
        if city_filter:
            errors = [e for e in errors if city_filter.lower() in e.get("city", "").lower()]

    if errors:
        # Group by (city, metric).
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for e in errors:
            groups[(e.get("city", ""), e.get("metric", ""))].append(e)

        print(f"\n{'─' * 60}")
        print(f"  Forecast accuracy by source (MAE = mean abs error in °F)")
        print(f"  {'City':<16}  {'Metric':<5}  {'N':>3}  "
              f"{'Ens':>6}  {'NWS':>6}  {'OWM':>6}  {'σ_meas':>7}")
        print(f"  {'-' * 58}")

        for (city, metric), recs in sorted(groups.items()):
            n = len(recs)
            def _mae(key):
                vals = [abs(float(r[key])) for r in recs if key in r]
                return f"{sum(vals)/len(vals):>6.2f}" if vals else f"{'—':>6}"
            ens_errs = [float(r["error_ensemble"]) for r in recs if "error_ensemble" in r]
            if len(ens_errs) >= 2:
                import statistics as _stats
                sigma_meas = f"{_stats.stdev(ens_errs):>7.2f}"
            else:
                sigma_meas = f"{'—':>7}"
            print(f"  {city:<16}  {metric:<5}  {n:>3}  "
                  f"{_mae('error_ensemble')}  {_mae('error_nws')}  "
                  f"{_mae('error_owm')}  {sigma_meas}")

    # ── Skip audit: would the bot have won the markets it skipped? ────────
    try:
        with open(_SIGNALS_FILE, "r", encoding="utf-8") as fh:
            skips = [
                s
                for s in json.load(fh)
                if s.get("action") in signal_learning_log.LEARNING_ACTIONS
                and s.get("would_have_won") is not None
            ]
    except (FileNotFoundError, json.JSONDecodeError):
        skips = []

    if city_filter:
        skips = [s for s in skips if city_filter.lower() in s.get("city", "").lower()]

    if skips:
        wins   = sum(1 for s in skips if s.get("would_have_won"))
        losses = len(skips) - wins
        rate   = wins / len(skips) * 100
        print(f"\n{'─' * 60}")
        print(f"  Skip / watch audit (signals without a filled position)")
        print(f"  Total audited skips: {len(skips)}")
        print(f"  Would-have-won:      {wins}  ({rate:.1f}%)")
        print(f"  Would-have-lost:     {losses}")
        if rate > 60:
            print(f"  → bot's signal quality is high — consider lowering MIN_EDGE_THRESHOLD")
        elif rate < 45:
            print(f"  → many skipped signals would have lost — filters are working")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Model calibration report for pavlov-weather-bot."
    )
    parser.add_argument("--city",  default=None,  help="Filter to a specific city")
    parser.add_argument("--min",   type=int, default=5,
                        help="Minimum trades required to show a bucket (default: 5)")
    args = parser.parse_args()
    run_report(city_filter=args.city, min_trades=args.min)


if __name__ == "__main__":
    main()
