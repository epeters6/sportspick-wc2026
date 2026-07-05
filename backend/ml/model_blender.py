"""
model_blender.py
Dynamic inverse-variance weighting for combining multiple probability
estimates (crowd consensus, MLB quant model, weather quant model, etc.)
into a single blended probability -- replacing the static 70/30 hardcoded
split currently mislabeled as "inverse-variance weighting" in consensus_engine.py.

CORE IDEA
---------
Each model produces a probability p_i for the same event. We treat each p_i
as a noisy estimate of the same latent quantity, converted to log-odds
(logit) space so that:
  (a) estimates are combined on an unbounded, roughly-symmetric-error scale --
      the same logit-space fix already applied to the MLB signal engine, and
  (b) the classic inverse-variance-weighted combination (the same math used
      in fixed-effect meta-analysis) is exactly the minimum-variance linear
      combination of independent unbiased estimates:

        combined_logit = sum(logit_i / var_i) / sum(1 / var_i)
        combined_prob  = sigmoid(combined_logit)

Each model's variance is estimated from an EXPONENTIALLY-WEIGHTED rolling
Brier score (mean squared error against realized 0/1 outcomes) -- not a flat
average over a fixed window. That matters: a flat window treats a bet from
199 games ago identically to yesterday's; an EWMA lets a model's "current
form" estimate respond faster to a real regime change (a genuine improvement
or a real degradation) while still being smoothed against single-bet noise.

SHRINKAGE
---------
With few resolved outcomes, an empirical Brier estimate is noisy (5 bets can
make a model look artificially great or terrible). We shrink each model's
estimated variance toward an uninformative prior (var = 0.25, i.e. the Brier
score of a coin-flip forecaster), in proportion to the model's EWMA
"effective sample size" -- which itself saturates at roughly 1/(1-decay) as
more data comes in. That saturation is a deliberate, useful property: it caps
how much confidence recency-weighting alone can produce, mirroring the
"don't fully trust a model until it has a track record" logic already applied
to the MLB pitcher-sample-size problem, generalized to every model in the blend.

WEIGHT CAP
----------
Even with plenty of data, no single model is allowed to fully dominate the
blend (default cap: 85%). Pure inverse-variance weighting will happily give
one model ~100% of the weight if its trailing Brier score is even slightly
better -- an overfit risk given these are noisy, comparatively small-sample
estimates. The cap keeps some diversification even when the numbers say
"just trust model X completely."

TUNING NOTE: PRIOR_STRENGTH, MAX_WEIGHT, and the half-life below are
reasonable starting points, not verified-correct constants -- same caveat
that applies to the hardcoded edges in the old MLB engine. Validate them the
same way: backtest sensitivity to each, don't just trust the defaults.
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

EPS = 1e-6                  # clip probabilities away from exactly 0/1
PRIOR_VARIANCE = 0.25        # Brier score of a coin-flip forecaster
PRIOR_STRENGTH = 20.0        # "pseudo-observations" of shrinkage toward the prior
MAX_WEIGHT = 0.85            # no single model may exceed this share of the blend
DEFAULT_HALF_LIFE = 40       # resolved outcomes for a squared error's influence to halve


def _clip(p: float) -> float:
    return min(max(p, EPS), 1 - EPS)


def _logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


@dataclass
class _ModelHistory:
    half_life: int = DEFAULT_HALF_LIFE
    ewma_sq_error: Optional[float] = None
    effective_n: float = 0.0

    def __post_init__(self):
        self._decay = 0.5 ** (1.0 / self.half_life)

    def record(self, predicted_prob: float, outcome: int) -> None:
        error = (predicted_prob - outcome) ** 2
        if self.ewma_sq_error is None:
            self.ewma_sq_error = error
        else:
            self.ewma_sq_error = self._decay * self.ewma_sq_error + (1 - self._decay) * error
        # Standard EWMA effective-sample-size recursion; saturates near 1/(1-decay).
        self.effective_n = self._decay * self.effective_n + 1.0

    def variance_estimate(self) -> float:
        if self.ewma_sq_error is None:
            return PRIOR_VARIANCE
        n = self.effective_n
        shrunk = (PRIOR_STRENGTH * PRIOR_VARIANCE + n * self.ewma_sq_error) / (PRIOR_STRENGTH + n)
        return max(shrunk, 1e-4)  # guard against a spuriously "perfect" model getting infinite weight

    @property
    def n_observations(self) -> float:
        return self.effective_n


class InverseVarianceBlender:
    """
    Usage:
        blender = InverseVarianceBlender()

        # after each market resolves, for every model that made a prediction on it:
        blender.record_outcome("mlb_quant", predicted_prob=0.63, outcome=1)
        blender.record_outcome("crowd_consensus", predicted_prob=0.58, outcome=1)

        # at prediction time:
        blended_prob, weights = blender.combine({
            "mlb_quant": 0.61,
            "crowd_consensus": 0.55,
        })
    """

    def __init__(self, half_life: int = DEFAULT_HALF_LIFE) -> None:
        self._half_life = half_life
        self._history: Dict[str, _ModelHistory] = {}

    def _get_history(self, model_name: str) -> _ModelHistory:
        if model_name not in self._history:
            self._history[model_name] = _ModelHistory(half_life=self._half_life)
        return self._history[model_name]

    def record_outcome(self, model_name: str, predicted_prob: float, outcome: int) -> None:
        """Call once a market resolves, for every model that predicted on it.
        outcome is 1 if the predicted outcome happened, else 0."""
        if outcome not in (0, 1):
            raise ValueError("outcome must be 0 or 1")
        self._get_history(model_name).record(_clip(predicted_prob), outcome)

    def reset_model(self, model_name: str) -> None:
        """Wipe a model's track record. Use this when a model gets rearchitected
        significantly enough that its old error history shouldn't carry over --
        otherwise a genuinely improved model stays underweighted by its
        predecessor's mistakes for a long time."""
        self._history.pop(model_name, None)

    def get_weights(self, model_names: List[str]) -> Dict[str, float]:
        if not model_names:
            return {}
        variances = {name: self._get_history(name).variance_estimate() for name in model_names}
        inv_var = {name: 1.0 / v for name, v in variances.items()}
        total = sum(inv_var.values())
        weights = {name: w / total for name, w in inv_var.items()}

        if len(weights) >= 2:
            for _ in range(len(weights)):
                over_cap = {k: v for k, v in weights.items() if v > MAX_WEIGHT}
                if not over_cap:
                    break
                excess = sum(v - MAX_WEIGHT for v in over_cap.values())
                for k in over_cap:
                    weights[k] = MAX_WEIGHT
                remaining = {k: v for k, v in weights.items() if k not in over_cap}
                remaining_total = sum(remaining.values())
                if remaining_total > 0:
                    for k in remaining:
                        weights[k] += excess * (remaining[k] / remaining_total)

        # Final safety-net normalization -- guards against floating-point
        # drift after several rounds of cap-and-redistribute leaving the
        # weights summing to 0.99999... instead of exactly 1.
        total_final = sum(weights.values())
        if total_final > 0:
            weights = {k: v / total_final for k, v in weights.items()}
        return weights

    def combine(self, model_probs: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
        """model_probs: {model_name: predicted_probability_for_this_market}
        Returns (blended_probability, weights_used)."""
        if not model_probs:
            raise ValueError("model_probs is empty")

        weights = self.get_weights(list(model_probs.keys()))

        weighted_logit_sum = sum(
            weights[name] * _logit(prob) for name, prob in model_probs.items()
        )
        blended_prob = _sigmoid(weighted_logit_sum)
        return blended_prob, weights

    def diagnostics(self, model_names: List[str]) -> Dict[str, dict]:
        """Useful for a dashboard: show each model's current variance,
        effective sample size, and derived weight -- so a human can sanity
        check the blend isn't quietly leaning on a model with almost no
        real track record."""
        weights = self.get_weights(model_names)
        out = {}
        for name in model_names:
            hist = self._get_history(name)
            out[name] = {
                "effective_n": round(hist.n_observations, 1),
                "variance_estimate": round(hist.variance_estimate(), 4),
                "weight": round(weights.get(name, 0.0), 4),
            }
        return out


if __name__ == "__main__":
    # Smoke test: one model is genuinely well-calibrated, one is basically
    # noise. A SINGLE 150-bet trajectory is exactly the kind of small-sample
    # read this whole project has learned to distrust (see: the significance
    # guardrails on P&L bucket analysis), so this test doesn't trust one --
    # it runs many independent simulated trajectories and reports the mean
    # and spread of the resulting weight, the same way you'd want to look at
    # any live A/B result before believing it.
    #
    # FINDING: near p=0.5 (every game close), most of a Brier score is
    # IRREDUCIBLE outcome variance (p*(1-p)), not model error -- so telling a
    # genuinely good model apart from noise is slow. A realistic mix of close
    # games and blowouts separates faster. Even so, at 150 resolved bets
    # neither scenario produces a dramatic split -- that's a feature, not a
    # bug: it means the blender won't overreact to a short streak, but it
    # also means you shouldn't expect (or force) fast convergence in a sport
    # where games are usually close.
    import random

    def one_trial(seed: int, low: float, high: float, n: int = 150) -> float:
        rng = random.Random(seed)
        blender = InverseVarianceBlender()
        for _ in range(n):
            true_prob = rng.uniform(low, high)
            outcome = 1 if rng.random() < true_prob else 0
            good_pred = _clip(true_prob + rng.gauss(0, 0.03))    # tight around truth
            noisy_pred = _clip(rng.uniform(0.2, 0.8))            # basically random
            blender.record_outcome("good_model", good_pred, outcome)
            blender.record_outcome("noisy_model", noisy_pred, outcome)
        return blender.get_weights(["good_model", "noisy_model"])["good_model"]

    def report(label: str, low: float, high: float, n_trials: int = 300) -> None:
        weights = [one_trial(seed, low, high) for seed in range(n_trials)]
        mean_w = sum(weights) / len(weights)
        std_w = (sum((w - mean_w) ** 2 for w in weights) / len(weights)) ** 0.5
        print(f"{label}: mean good_model weight over {n_trials} independent "
              f"150-bet trajectories = {mean_w:.3f} (std {std_w:.3f})")

    report("Close games only  (true_prob in [0.30, 0.70])", 0.30, 0.70)
    report("Realistic mix     (true_prob in [0.05, 0.95])", 0.05, 0.95)

    print()
    print("Takeaway: same blender, same code -- how fast it can tell a good model")
    print("from noise depends heavily on how lopsided the markets it's learning")
    print("from actually are. In a close-games sport, budget for a lot more")
    print("resolved bets before trusting the weights than these 150 provide.")

def build_blender_from_db() -> InverseVarianceBlender:
    """
    Rebuilds the blender state by replaying all resolved model predictions
    from the database in chronological order.
    """
    from backend.db import get_db
    blender = InverseVarianceBlender()
    db = get_db()
    
    # Fetch resolved matches
    matches = db.table("matches").select("id, winner, home_team, away_team").eq("is_final", True).not_.is_("winner", "null").execute().data or []
    if not matches:
        return blender
        
    match_map = {m["id"]: m for m in matches}
    match_ids = list(match_map.keys())
    
    # We need to process chronologically. Since we can't easily fetch 100k rows if it grows, 
    # we limit to the last 2000 resolved matches.
    if len(match_ids) > 2000:
        # Sort by id assuming UUIDv7 or we need to sort by created_at.
        # Actually matches doesn't have resolved_at, so we just use the last 2000.
        match_ids = match_ids[-2000:]
    
    # Batch fetch predictions
    preds = []
    chunk_size = 200
    for i in range(0, len(match_ids), chunk_size):
        chunk = match_ids[i:i+chunk_size]
        p = db.table("model_predictions").select("*").in_("event_key", chunk).execute().data or []
        preds.extend(p)
        
    # Sort predictions by created_at
    preds.sort(key=lambda x: x.get("created_at", ""))
    
    for p in preds:
        m = match_map.get(p["event_key"])
        if not m:
            continue
            
        predicted_outcome = p.get("outcome", "").strip().lower()
        actual_winner = (m.get("winner") or "").strip().lower()
        if not predicted_outcome or not actual_winner:
            continue
            
        is_correct = 1 if predicted_outcome == actual_winner else 0
        prob = float(p.get("prob", 0.0))
        
        # We only record the model if we have a valid probability
        source = p.get("source", "unknown")
        blender.record_outcome(source, prob, is_correct)
        
    return blender
