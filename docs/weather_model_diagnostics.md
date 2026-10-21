# Weather model diagnosis and prospective market guard

The October 21 audit compares the same saved, officially graded forward
probability vectors. Models must have been trained before decisions and all
weather, observation and quote receipts must precede their decisions. There are
4,018 matched vectors across 319 station/date events and 17 target dates.
Repeated snapshots, high/low and venues share each station/date's weight.
Cities can still have correlated weather; this is not 319 independent days.

| Saved forecast | Brier, lower is better | Log loss, lower is better |
| --- | ---: | ---: |
| Market | 0.414651 | 0.836817 |
| Regularized logistic | 0.441526 | 0.870380 |
| Boosted trees | 0.494410 | 0.975264 |

The loss is broad across both venues and forecast horizons. The exact Brier
difference is `2 (market - outcome) * adjustment + adjustment^2`, summed across
buckets and averaged with the same weights. Adjustment magnitude accounts for
97% of logistic's excess Brier and 89% of trees' excess Brier. The remaining
directional term also hurts rather than offsets the penalty in aggregate.
This decomposition is descriptive attribution, not proof of a causal mechanism.

The existing v3 selection must choose logistic or trees; market is not an
eligible fallback. The latest saved training report selected trees on four
calibration dates with only about 0.00049 log-loss separation from logistic.
That comparison does not establish that either learned model beats the market.
The original implementation is frozen and remains unchanged for comparison.

## Separate prospective correction

`weather_market_guard_v1` starts with market probabilities and evaluates only
small blends with already-saved forward challenger probabilities. It uses the
latest ten officially graded target dates available at fitting time, requires
at least seven dates and 75 station/date events, and caps any bucket change at
five percentage points. Both Brier and log loss must improve by at least 0.001
on calibration, with both improving on at least 60% of its dates. Otherwise the
forecast is exactly the market vector. These are exploratory screening rules,
not statistical proof or a trading authorization.

On the October 21 evidence, the rule selects **market, weight zero**. A capped
25% logistic blend improves calibration Brier by only 0.000598, below the fixed
0.001 minimum. Do not lower the threshold to manufacture a passing result.
This selection is not an independent test: these outcomes have been inspected.

The runner fits or restores this policy before collection and logs future
unlabelled decisions separately. It rejects backdated predictions, stale
collection files, post-window recording, incompatible models and conflicting
immutable evidence. The existing v3 predictions and database rows remain
untouched. A guard failure is visible in the persisted stage report while the
original collection can still run. Both modes remain shadow-only.

The separate `weather-market-guard-*` Actions artifact retains the exact model,
specification and forecast receipts for 90 days. It contains original row IDs,
decision and recording times, base-model and guard-model identity, market
baseline and probabilities. Grade these saved forecasts against later official
outcomes using station/date weights; archive them before retention expires.
Do not regenerate past guard forecasts after labels arrive or equate a green
workflow with persisted forecast evidence.

## What remains unresolved

- The full April 3–October 19 archive and fixed retest are still incomplete.
  Temperature corrections, retrospective market scores and future predictions
  must remain distinct evidence streams.
- Existing quote receipts contain missing cost/depth or fee-timestamp evidence.
  Missing historical information stays missing; current quotes cannot repair it.
- Matching market accuracy is not a profitable edge after fees, spread and
  fills. A new adjustment needs subsequent forward evaluation and executable
  cost evidence before any profitability conclusion.

Reproduce locally with `python -m scripts.diagnose_weather_models --history
<saved-market-input.json> --output <new-diagnostic-directory>`. This reads local
evidence only and creates a separate policy artifact; it does not deploy a model,
change frozen experiments, reset losses, place orders or refetch history.
