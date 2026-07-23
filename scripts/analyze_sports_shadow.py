import json
import math
import os
from collections import defaultdict
import statistics
from typing import Any, Optional

# Exact Phase-4 contaminated candidate_ids only (reused Jul-22 probs on Jul-23 slate).
CLV_EVAL_EXCLUDE_CANDIDATE_IDS = frozenset(
    {
        "sports_mlb_ml_2026-07-23_Toronto Blue Jays_Tampa Bay Rays_1784721382",
        "sports_mlb_ml_2026-07-23_Atlanta Braves_San Diego Padres_1784721409",
    }
)


def _clv_excluded(candidate_id: str | None) -> bool:
    return (candidate_id or "") in CLV_EVAL_EXCLUDE_CANDIDATE_IDS


def fetch_durable_clv_obligations(db=None) -> list[dict]:
    """Load CLV obligations from Supabase (shared across Actions runners)."""
    try:
        from backend.db import get_db

        db = db or get_db()
        return (
            db.table("clv_obligations")
            .select(
                "candidate_id, platform, market_id, outcome_id, side, "
                "entry_price, entry_market_price, entry_effective_cost, entry_ts, "
                "status_15m, status_1h, status_close, "
                "obs_15m_price, obs_1h_price, obs_close_price, "
                "obs_15m_ts, obs_1h_ts, obs_close_ts, "
                "book_ts_15m, book_ts_1h, book_ts_close, "
                "event_id, event_start, model_prob, market_prob, selected_team, "
                "home_team, away_team, match_id, game_pk, shares, stake, "
                "settlement_status, settlement_result, settlement_pnl, settled_at, "
                "settlement_source, metadata"
            )
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def export_clv_obligations(
    rows: list[dict],
    filepath: str = "reports/sports_shadow/clv_obligations_export.json",
) -> Optional[str]:
    """Write durable CLV rows to a local export artifact for the validation report."""
    try:
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(rows, f, indent=2, default=str)
        return filepath
    except OSError:
        return None


def _entry_market(row: dict) -> float:
    if row.get("entry_market_price") is not None:
        return float(row["entry_market_price"])
    return float(row.get("entry_price") or 0.0)


def summarize_durable_clv(rows: list[dict]) -> dict[str, Any]:
    """Compute CLV metrics from durable Supabase obligations (not runner-local JSONL)."""
    clv_15m_vals: list[float] = []
    clv_1h_vals: list[float] = []
    closing_line_beaten = 0
    clv_records_count = 0
    clv_excluded_count = 0
    status_counts = {
        "observed_15m": 0,
        "observed_1h": 0,
        "observed_close": 0,
        "unavailable_15m": 0,
        "unavailable_1h": 0,
        "unavailable_close": 0,
        "pending_15m": 0,
        "pending_1h": 0,
        "pending_close": 0,
    }

    for row in rows:
        cid = row.get("candidate_id")
        if _clv_excluded(cid):
            clv_excluded_count += 1
            continue
        clv_records_count += 1
        entry = _entry_market(row)
        for cp in ("15m", "1h", "close"):
            st = row.get(f"status_{cp}") or "pending"
            key = f"{st}_{cp}"
            status_counts[key] = status_counts.get(key, 0) + 1

        if row.get("status_15m") == "observed" and row.get("obs_15m_price") is not None:
            clv_15m_vals.append(float(row["obs_15m_price"]) - entry)
        if row.get("status_1h") == "observed" and row.get("obs_1h_price") is not None:
            clv_1h_vals.append(float(row["obs_1h_price"]) - entry)
        if row.get("status_close") == "observed" and row.get("obs_close_price") is not None:
            if float(row["obs_close_price"]) > entry:
                closing_line_beaten += 1

    return {
        "average_clv_15m": statistics.mean(clv_15m_vals) if clv_15m_vals else 0,
        "average_clv_1h": statistics.mean(clv_1h_vals) if clv_1h_vals else 0,
        "closing_line_beaten_rate_if_available": (
            closing_line_beaten / clv_records_count if clv_records_count > 0 else 0
        ),
        "clv_records_evaluated": clv_records_count,
        "clv_records_excluded_reused_prob": clv_excluded_count,
        "clv_status_counts": status_counts,
        "clv_observed_15m_n": len(clv_15m_vals),
        "clv_observed_1h_n": len(clv_1h_vals),
        "clv_source": "supabase_clv_obligations",
    }


def _outcome_metrics(rows: list[dict]) -> dict[str, Any]:
    observations: list[tuple[float, float, float]] = []
    for row in rows:
        if row.get("settlement_status") not in ("won", "lost"):
            continue
        if not isinstance(row.get("settlement_result"), bool):
            continue
        try:
            model_p = float(row.get("model_prob"))
            market_p = float(row.get("market_prob"))
        except (TypeError, ValueError):
            continue
        if not (0.0 <= model_p <= 1.0 and 0.0 <= market_p <= 1.0):
            continue
        observations.append(
            (model_p, market_p, 1.0 if row["settlement_result"] else 0.0)
        )

    n = len(observations)
    if n == 0:
        return {
            "settled_model_n": 0,
            "model_brier": None,
            "market_brier": None,
            "model_log_loss": None,
            "market_log_loss": None,
            "brier_delta_vs_market": None,
            "log_loss_delta_vs_market": None,
        }

    def brier(index: int) -> float:
        return sum((obs[index] - obs[2]) ** 2 for obs in observations) / n

    def log_loss(index: int) -> float:
        total = 0.0
        for obs in observations:
            probability = min(max(obs[index], 1e-6), 1.0 - 1e-6)
            outcome = obs[2]
            total += -(
                outcome * math.log(probability)
                + (1.0 - outcome) * math.log(1.0 - probability)
            )
        return total / n

    model_brier = brier(0)
    market_brier = brier(1)
    model_log_loss = log_loss(0)
    market_log_loss = log_loss(1)
    return {
        "settled_model_n": n,
        "model_brier": model_brier,
        "market_brier": market_brier,
        "model_log_loss": model_log_loss,
        "market_log_loss": market_log_loss,
        "brier_delta_vs_market": model_brier - market_brier,
        "log_loss_delta_vs_market": model_log_loss - market_log_loss,
    }


def summarize_settled_outcomes(rows: list[dict]) -> dict[str, Any]:
    settled = [
        row
        for row in rows
        if row.get("settlement_status") in ("won", "lost")
    ]

    def metadata_value(row: dict, key: str, default: str = "unknown") -> str:
        metadata = row.get("metadata") or {}
        value = metadata.get(key) if isinstance(metadata, dict) else None
        return str(value or default)

    def probability_bucket(row: dict) -> str:
        try:
            probability = float(row.get("model_prob"))
        except (TypeError, ValueError):
            return "unknown"
        if probability < 0.40:
            return "under_0.40"
        if probability < 0.60:
            return "0.40_to_0.60"
        return "0.60_and_over"

    groupers = {
        "by_model_version": lambda row: metadata_value(row, "model_version"),
        "by_platform": lambda row: str(row.get("platform") or "unknown"),
        "by_probability_bucket": probability_bucket,
        "by_selected_team": lambda row: str(row.get("selected_team") or "unknown"),
        "by_side": lambda row: str(row.get("side") or "unknown"),
    }
    groups: dict[str, dict[str, Any]] = {}
    for label, key_fn in groupers.items():
        buckets: dict[str, list[dict]] = defaultdict(list)
        for row in settled:
            buckets[key_fn(row)].append(row)
        groups[label] = {
            key: _outcome_metrics(bucket_rows)
            for key, bucket_rows in sorted(buckets.items())
        }
    return {**_outcome_metrics(settled), "settled_outcome_groups": groups}


def run_analysis(
    decisions_file="sports_shadow_decisions.jsonl",
    fills_file="sports_paper_fills.jsonl",
    clv_file="sports_clv_tracking.jsonl",
    *,
    db=None,
    clv_obligations: list[dict] | None = None,
    export_clv_path: str | None = "reports/sports_shadow/clv_obligations_export.json",
):
    if not os.path.exists(decisions_file):
        raise FileNotFoundError(
            f"Missing sports shadow decisions manifest: {decisions_file}"
        )

    total_predictions = 0
    total_rejections = 0
    total_would_trade = 0

    rejection_reason_counts = defaultdict(int)
    calibration_status_counts = defaultdict(int)
    coefficient_source_counts = defaultdict(int)

    timestamp_missing_count = 0
    timestamp_assumed_count = 0

    model_probs = []
    market_probs = []
    edges_before = []
    net_edges_after = []
    executable_costs = []
    fees_per_share = []
    visible_depths = []

    by_sport = defaultdict(int)
    by_league = defaultdict(int)
    by_market_type = defaultdict(int)
    by_platform = defaultdict(int)
    by_model_version = defaultdict(int)

    with open(decisions_file, "r") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            total_predictions += 1

            rejection_reason = d.get("rejection_reason")
            if rejection_reason:
                total_rejections += 1
                rejection_reason_counts[rejection_reason] += 1
            else:
                total_would_trade += 1

            cal_status = d.get("calibration_status", "unknown")
            calibration_status_counts[cal_status] += 1

            coef_src = d.get("coefficient_source", "unknown")
            coefficient_source_counts[coef_src] += 1

            if not d.get("received_timestamp"):
                timestamp_missing_count += 1

            if rejection_reason == "ORDERBOOK_TIMESTAMP_ASSUMED_FOR_SHADOW":
                timestamp_assumed_count += 1

            if "P_model" in d and d["P_model"] is not None:
                model_probs.append(d["P_model"])
            if "P_market" in d and d["P_market"] is not None:
                market_probs.append(d["P_market"])
            if "edge_before_execution" in d and d["edge_before_execution"] is not None:
                edges_before.append(d["edge_before_execution"])
            if "net_edge_after_execution" in d and d["net_edge_after_execution"] is not None:
                net_edges_after.append(d["net_edge_after_execution"])
            if "executable_cost" in d and d["executable_cost"] is not None:
                executable_costs.append(d["executable_cost"])
            if "fee_per_share" in d and d["fee_per_share"] is not None:
                fees_per_share.append(d["fee_per_share"])
            if "visible_depth" in d and d["visible_depth"] is not None:
                visible_depths.append(d["visible_depth"])

            by_sport[d.get("sport", "unknown")] += 1
            by_league[d.get("league", "unknown")] += 1
            by_market_type[d.get("market_type", "unknown")] += 1
            if d.get("sized_order") and d["sized_order"].get("candidate"):
                by_platform[d["sized_order"]["candidate"].get("platform", "unknown")] += 1
            by_model_version[d.get("model_version", "unknown")] += 1

    total_paper_fills = 0
    paper_fill_sizes = []

    if os.path.exists(fills_file):
        with open(fills_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                if not d.get("rejection_reason"):
                    total_paper_fills += 1
                    if "filled_shares" in d:
                        paper_fill_sizes.append(d["filled_shares"])

    if clv_obligations is None:
        clv_obligations = fetch_durable_clv_obligations(db=db)
    clv_summary = summarize_durable_clv(clv_obligations)
    outcome_summary = summarize_settled_outcomes(clv_obligations)
    export_path = None
    if export_clv_path and clv_obligations:
        export_path = export_clv_obligations(clv_obligations, export_clv_path)

    report = {
        "total_predictions": total_predictions,
        "total_rejections": total_rejections,
        "total_would_trade": total_would_trade,
        "total_paper_fills": total_paper_fills,
        "rejection_reason_counts": dict(rejection_reason_counts),
        "average_model_prob": statistics.mean(model_probs) if model_probs else 0,
        "average_market_prob": statistics.mean(market_probs) if market_probs else 0,
        "average_edge_before_execution": statistics.mean(edges_before) if edges_before else 0,
        "average_net_edge_after_execution": statistics.mean(net_edges_after) if net_edges_after else 0,
        "average_executable_cost": statistics.mean(executable_costs) if executable_costs else 0,
        "average_fee_per_share": statistics.mean(fees_per_share) if fees_per_share else 0,
        "average_visible_depth": statistics.mean(visible_depths) if visible_depths else 0,
        "average_paper_fill_size": statistics.mean(paper_fill_sizes) if paper_fill_sizes else 0,
        **clv_summary,
        **outcome_summary,
        "clv_export_path": export_path,
        "calibration_status_counts": dict(calibration_status_counts),
        "coefficient_source_counts": dict(coefficient_source_counts),
        "timestamp_missing_count": timestamp_missing_count,
        "timestamp_assumed_count": timestamp_assumed_count,
        "groups": {
            "by_sport": dict(by_sport),
            "by_league": dict(by_league),
            "by_market_type": dict(by_market_type),
            "by_platform": dict(by_platform),
            "by_model_version": dict(by_model_version),
            "by_calibration_status": dict(calibration_status_counts),
            "by_rejection_reason": dict(rejection_reason_counts)
        }
    }
    return report

if __name__ == "__main__":
    report = run_analysis()
    print(json.dumps(report, indent=2))
