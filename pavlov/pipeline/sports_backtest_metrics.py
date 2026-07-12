import math
from typing import List

def log_loss(y_true: List[int], y_pred: List[float]) -> float:
    if not y_true: return 0.0
    epsilon = 1e-15
    loss = 0.0
    for yt, yp in zip(y_true, y_pred):
        yp = max(epsilon, min(1 - epsilon, yp))
        if yt == 1:
            loss -= math.log(yp)
        else:
            loss -= math.log(1 - yp)
    return loss / len(y_true)

def brier_score(y_true: List[int], y_pred: List[float]) -> float:
    if not y_true: return 0.0
    score = 0.0
    for yt, yp in zip(y_true, y_pred):
        score += (yp - yt) ** 2
    return score / len(y_true)

def calibration_bins(y_true: List[int], y_pred: List[float], bins: int = 10) -> dict:
    if not y_true: return {}
    # Simple equal-width bins
    bin_counts = [0] * bins
    bin_sums = [0.0] * bins
    bin_preds = [0.0] * bins
    
    for yt, yp in zip(y_true, y_pred):
        b = min(bins - 1, int(yp * bins))
        bin_counts[b] += 1
        bin_sums[b] += yt
        bin_preds[b] += yp
        
    result = {}
    for i in range(bins):
        if bin_counts[i] > 0:
            result[f"{i/bins:.1f}-{(i+1)/bins:.1f}"] = {
                "count": bin_counts[i],
                "mean_pred": bin_preds[i] / bin_counts[i],
                "mean_true": bin_sums[i] / bin_counts[i]
            }
    return result

def mean_clv(clvs: List[float]) -> float:
    if not clvs: return 0.0
    return sum(clvs) / len(clvs)

def roi_after_fees(pnl: float, capital_risked: float) -> float:
    if capital_risked == 0: return 0.0
    return pnl / capital_risked

def closing_line_beaten_rate(clvs: List[float]) -> float:
    if not clvs: return 0.0
    beaten = sum(1 for c in clvs if c > 0)
    return beaten / len(clvs)
