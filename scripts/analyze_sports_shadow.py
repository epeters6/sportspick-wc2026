import json
import os
from collections import defaultdict
import statistics

def run_analysis(decisions_file="sports_shadow_decisions.jsonl", fills_file="sports_paper_fills.jsonl", clv_file="sports_clv_tracking.jsonl"):
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
    
    # Groups
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
                        
    clv_15m_vals = []
    clv_1h_vals = []
    closing_line_beaten = 0
    clv_records_count = 0
    
    if os.path.exists(clv_file):
        with open(clv_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                clv_records_count += 1
                if "price_after_15m" in d and d["price_after_15m"] is not None:
                    clv_15m_vals.append(d["price_after_15m"] - d.get("entry_price", 0))
                if "price_after_1h" in d and d["price_after_1h"] is not None:
                    clv_1h_vals.append(d["price_after_1h"] - d.get("entry_price", 0))
                if "closing_price" in d and d["closing_price"] is not None:
                    if d["closing_price"] > d.get("entry_price", 0):
                        closing_line_beaten += 1

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
        "average_clv_15m": statistics.mean(clv_15m_vals) if clv_15m_vals else 0,
        "average_clv_1h": statistics.mean(clv_1h_vals) if clv_1h_vals else 0,
        "closing_line_beaten_rate_if_available": closing_line_beaten / clv_records_count if clv_records_count > 0 else 0,
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
