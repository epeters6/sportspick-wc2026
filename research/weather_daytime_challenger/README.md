# Separate daytime uncertainty and market challenger

This is an offline research candidate. It does not modify the frozen October 23–November 5 daytime paper model, initialize a paper account, write prospective forecasts, or change the hosted historical backfill. No network downloads are performed.

## What changes

The reference mean remains PR20 residual recentering. A symmetric, monotone transformation replaces the PR19 Gaussian shape. It estimates the 50%, 80%, 90%, and 95% absolute-error quantiles from earlier standardized prediction errors, with a 60-day window and station estimates shrunk toward the pool. Central width and outer width can change by different amounts. Width changes are bounded; insufficient station history retains the reference distribution. The method may widen as well as narrow a range.

Every mean used to construct a calibration error excludes that error's own outcome. The shape fit then excludes the current target and uses the same assumed three-day label lag. This is retrospective timing discipline, not proof of historical forecast or label availability.

The market guard considers weights of 0%, 10%, 25%, and 50% toward the calibrated weather probabilities, with at most a five-percentage-point change to any bucket. A positive adjustment needs earlier evidence in its market-probability band as well as overall Brier and log-loss improvement. Support counts and scores share station/date weight across venues. Calendar-block uncertainty and improvement across dates must pass; missing support defaults to the saved market vector. These gates are conservative heuristics, not guarantees of profitability. With a five-point quote-edge threshold and a five-point adjustment cap, the guarded candidate will frequently abstain after costs.

The fixed market calibration cohort is September 21–29. Only official labels available before the first October 1-target test decision can enter selection. The guard stays fixed for the October 1–18 test cohort. Only the first valid saved next-day high decision per venue/station/date is evaluated. Saved market probabilities, original model predictions, contracts, labels, and costs are retained; no historical quotes are reconstructed.

## October 23 offline result

The completed GFS source has 4,153 station/date events across 21 stations and 198 dates. Temperature evaluation uses 608 events across September 20–October 18 (29 dates):

| Metric | Reference | Calibrated shape |
| --- | ---: | ---: |
| Coverage of nominal 50% interval | 60.36% | 51.15% |
| Coverage of nominal 80% interval | 84.38% | 79.28% |
| Coverage of nominal 90% interval | 92.43% | 88.49% |
| Coverage of nominal 95% interval | 95.56% | 94.90% |
| Temperature distribution error (CRPS, lower is better) | 1.68988 | 1.67766 |
| Mean absolute temperature error | 2.30949°F | 2.30949°F |

The central range is better calibrated. The 90% range is now somewhat too narrow, and its interval score slightly worsens. The CRPS improvement is about 0.72%; its seven-day block-bootstrap difference interval is [-0.03107, +0.00036], so a sustained improvement is not established. There are only five calendar blocks.

Market calibration has 153 station/date events across nine dates. None of the adjustment weights passes the support gates. For contracts in the below-5% market-probability band, increasing the weather weight worsens calibration Brier scores at all three tested weights.

The later market test contains only **76 independent station/date events, 96 venue vectors, and four dates (October 1–4)**. October 5–18 has no matching saved forward market history; October 20–21 has no match in this completed GFS source. Missing observations are excluded, never filled with current prices.

| Probability score | Saved market | Saved original model | Reference daytime replay | Calibrated shape | Guarded challenger |
| --- | ---: | ---: | ---: | ---: | ---: |
| Brier, lower is better | 0.68890 | 0.74312 | 0.84501 | 0.84082 | 0.68890 |
| Log loss, lower is better | 1.35349 | 1.45042 | 1.81865 | 1.79828 | 1.35349 |

The unguarded candidate improves slightly but still loses to the saved market. The guard chooses **zero weather weight**, matches the market, and selects **zero hypothetical trades**. Matching the market and abstaining are not evidence of a new edge. The reference is a rolling reconstruction of the daytime method; these are not results from the live frozen paper cohort.

Unguarded quote-only diagnostics at saved costs are also unprofitable: calibrated shape is -$0.99 over 72 Kalshi hypothetical one-contract positions and -$2.395 over 15 Polymarket positions. These are separate venue sensitivities, not executed fills or bankroll simulations. The report retains 1¢/3¢ additional-slippage scenarios and five missing Kalshi cost buckets. No real-money or paper promotion is performed.

## Reproduce

Set `PAVLOV_BYPASS_CONFIG=1`, `OMP_NUM_THREADS=1`, and `OPENBLAS_NUM_THREADS=1`, then run:

```text
python -m scripts.run_weather_daytime_challenger \
  --history <completed-GFS-directory> \
  --market-history <saved-weather_segment_input.json> \
  --output <separate-output-directory>
```

The output contains a fixed specification, a content-addressed replay with all fitted states and matched saved market evidence, and a report with an integrity hash. A matching existing report is verified and reused. Corrupt or mismatched evidence fails closed and is not erased. `result_summary.json` contains the compact result and source hashes; full evidence stays in the local reports directory.

Validation: 72 local tests passed across the challenger, frozen daytime paper implementation, market guard, residual/condition calibration, and backfill modules. A second invocation verified and reused the complete replay. Hosted unit-test CI remains disabled; this is local verification.

Months and outcomes were previously inspected during earlier research. The replay is exploratory, has unknown historical publication timing, and provides no unseen-holdout, forward-profit, or live-readiness claim. A future prospective challenger would need separate immutable forecasts recorded before outcomes; this change does not launch one.
