from dataclasses import dataclass, field
from typing import List, Optional
import hashlib

@dataclass
class SourceQualityRecord:
    source_id: str
    sport: str
    market_type: str
    pick_count: int
    avg_clv: float
    median_clv: float
    hit_rate: float
    roi: float
    sample_size: int
    last_30d_clv: float
    last_90d_clv: float
    closing_line_beaten_rate: float
    correlation_cluster_id: str
    confidence_weight: float
    has_valid_timestamps: bool = True

def estimate_source_weight(record: SourceQualityRecord, k: int = 100, min_sample: int = 5, max_weight: float = 0.5) -> float:
    """
    Bayesian shrinkage.
    source_skill = n / (n + k) * observed_clv_skill
    We assume the prior CLV is 0.
    """
    if not record.has_valid_timestamps:
        return 0.0
    if record.sample_size < min_sample:
        return 0.0
        
    shrinkage_factor = record.sample_size / (record.sample_size + k)
    
    # We ignore raw ROI. We weight strictly on CLV. 
    # If CLV is negative, weight is <= 0.
    shrunk_clv = record.avg_clv * shrinkage_factor
    
    # A multiplier to make it meaningful for the logistic model
    # E.g. +0.02 CLV on 500 sample size => 0.02 * (500/600) => 0.016
    weight = shrunk_clv
    return min(max(weight, -max_weight), max_weight)

def update_source_pick_result(record: SourceQualityRecord, new_clv: float, new_roi: float, is_hit: bool) -> SourceQualityRecord:
    n = record.sample_size
    record.avg_clv = ((record.avg_clv * n) + new_clv) / (n + 1)
    record.roi = ((record.roi * n) + new_roi) / (n + 1)
    record.hit_rate = ((record.hit_rate * n) + (1.0 if is_hit else 0.0)) / (n + 1)
    record.sample_size += 1
    record.pick_count += 1
    
    if new_clv > 0.0:
        record.closing_line_beaten_rate = ((record.closing_line_beaten_rate * n) + 1.0) / (n + 1)
    else:
        record.closing_line_beaten_rate = (record.closing_line_beaten_rate * n) / (n + 1)
        
    record.confidence_weight = estimate_source_weight(record)
    return record

@dataclass
class DedupedPickSet:
    raw_pick_count: int
    independent_source_count: int
    duplicate_group_count: int
    deduped_weighted_signal: float
    duplicate_groups: List[List[str]]

def deduplicate_picks(picks: List[dict], source_quality_table: dict) -> DedupedPickSet:
    """
    Groups picks by identical source or correlation cluster.
    Only counts the cluster once, taking the max weight or average.
    """
    clusters = {}
    
    for pick in picks:
        source_id = pick.get("source_id", "unknown")
        market_side = f"{pick.get('market_id')}_{pick.get('side')}"
        record = source_quality_table.get(source_id)
        cluster_id_base = record.correlation_cluster_id if record and record.correlation_cluster_id else source_id
        
        # Hash text/link for near-identical duplicate detection
        text_hash = ""
        if "raw_text" in pick and pick["raw_text"]:
            text_hash = hashlib.md5(pick["raw_text"][:50].encode()).hexdigest()
        link = pick.get("link", "")
        
        if text_hash or link:
            cluster_id = f"content_{market_side}_{text_hash}_{link}"
        else:
            cluster_id = f"source_{cluster_id_base}_{market_side}"
        
        if cluster_id not in clusters:
            clusters[cluster_id] = []
        clusters[cluster_id].append(pick)
        
    deduped_weighted_signal = 0.0
    independent_source_count = len(clusters)
    duplicate_group_count = sum(1 for c in clusters.values() if len(c) > 1)
    
    for cluster_id, cluster_picks in clusters.items():
        # Take the best source weight in the cluster to represent it
        cluster_weights = []
        for p in cluster_picks:
            src = source_quality_table.get(p.get("source_id"))
            w = estimate_source_weight(src) if src else 0.0
            cluster_weights.append(w)
            
        deduped_weighted_signal += max(cluster_weights) if cluster_weights else 0.0
        
    return DedupedPickSet(
        raw_pick_count=len(picks),
        independent_source_count=independent_source_count,
        duplicate_group_count=duplicate_group_count,
        deduped_weighted_signal=deduped_weighted_signal,
        duplicate_groups=[[p.get("source_id") for p in cp] for cp in clusters.values()]
    )

def aggregate_source_signal(picks: List[dict], source_quality_table: dict) -> float:
    deduped = deduplicate_picks(picks, source_quality_table)
    return deduped.deduped_weighted_signal
