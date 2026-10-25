# Correction reliability and conditional uncertainty challenger

This is a separate offline research candidate. It does not change the frozen
October 23–November 5 daytime paper test, the PR24 verified-run shadow recipe,
the v2/v3 experiments, or the hosted historical backfill. It contains no network
collector, account access, order placement, or automatic promotion.

## Fixed method

For each station, consider 0%, 25%, 50%, 75%, and 100% of the rolling PR20
temperature correction. Each earlier correction was itself calculated without
its own target outcome. Use the most recent 60 days of those errors, with the
existing assumed three-day label lag. Require at least 21 station/date events,
a negative upper bound on the station's MAE difference from raw GFS, and no MSE
deterioration. Seven-day calendar blocks preserve serial dependence. Pooled
station risk regularizes the ranking of qualifying weights. If none qualifies,
retain the raw forecast. The bootstrap gates are research heuristics; they do
not establish statistical validity after repeated model selection.

Recalibrate the PR23 monotone probability shape using errors of the earlier
reliability-adjusted forecasts. Forecast high-minus-low range defines fixed
bands below 10°F, 10–20°F, and at least 20°F. Supported contexts use station
quantiles shrunk toward a pooled context distribution. Sparse contexts fall
back to the global shape. Both widening and narrowing are possible; the PR23
shape bounds and probability safeguards remain unchanged.

Forecast age is supported only when an input has a verified forecast receipt
before an actual decision and an initialization before that receipt. Fixed age
bands are below 12 hours, 12–24 hours, and at least 24 hours. An age subgroup
needs at least 60 events across seven dates, plus station support for the shape.
Missing age is explicitly unknown; an unsupported age uses the range/global
fallback. Archive run IDs alone never create a decision timestamp or age label.
Tests exercise known-age learning, sparse-age backoff, and invalid timing.
The completed archive has no actual decision times, so **zero historical
predictions use an estimated age effect**. PR24's prospective observations must
mature before an age effect can be evaluated. This change does not replace
PR24's fixed arms or retrospectively write challenger forecasts into that study.

The market comparison retains exact saved contracts, quotes, official labels,
and first eligible local next-day decisions. Venues share station/date weight.
The unchanged PR23 market guard permits only bounded, supported adjustments
selected from earlier official labels. No price history is invented.

## October 25 exploratory result

The completed GFS source contains 4,153 station/date events across all 21
stations and 198 dates. It is distinct from the unfinished hosted multi-model
archive. Temperature evaluation covers 608 events over September 20–October 18,
29 dates. These months were inspected in previous research; this is not an
untouched test set or a test of the frozen paper account.

| Metric (lower error is better) | PR23 | New challenger |
| --- | ---: | ---: |
| Temperature MAE | 2.30949°F | 2.33836°F |
| Distribution error (CRPS) | 1.67766 | 1.71183 |
| Nominal 50% interval coverage | 51.15% | 51.32% |
| Nominal 80% interval coverage | 79.28% | 78.62% |
| Nominal 90% interval coverage | 88.49% | 87.34% |
| Nominal 95% interval coverage | 94.90% | 93.26% |

The MAE difference is +0.02887°F, with a seven-day block interval of
[-0.04262, +0.12884]. The CRPS difference is +0.03417, interval
[-0.02197, +0.10559]. Only five calendar blocks are present. Temperature results
do not support promotion. Range conditioning also fails to improve on the
challenger's global shape: CRPS difference +0.00348, interval
[-0.00214, +0.00853]. All ablations and all stations remain in the full report.

Correction weights across the 608 evaluation events were 0%: 214, 25%: 24,
50%: 63, 75%: 186, and 100%: 121. Suppressing corrections was not uniformly
beneficial; the individual San Francisco example that motivated investigation
is not a station-exclusion rule or a fitting target.

The matched market test still has only 76 station/date events and 96 venue
vectors across October 1–4. October 5–18 has no matching saved market forecasts;
later markets do not match the completed GFS archive. Missing rows remain gaps.

| Probability score | Market | PR23 | New challenger | Guarded |
| --- | ---: | ---: | ---: | ---: |
| Brier | 0.68890 | 0.84082 | 0.82809 | 0.68890 |
| Log loss | 1.35349 | 1.79828 | 1.77279 | 1.35349 |

The unguarded challenger improves this small market sample but still loses to
the market. The guard selects zero model weight and zero hypothetical trades.
At saved quoted costs, the unguarded challenger has -$1.25 over 70 Kalshi
hypothetical contracts and -$2.90 over 16 Polymarket contracts, both worse than
PR23's quote-only sensitivity. Five Kalshi buckets lack complete cost evidence.
The report retains separate venue results and 1¢/3¢ additional-slippage cases.
These are not executed fills, bankroll simulations, or realized profit.

**Decision: preserve as an offline candidate, no model promotion.** Better
calibration and profitable selection must be demonstrated on new prospective
evidence; matching the market is not an edge. October 23–24 inspected paper
outcomes were not included in this fit or replay. Any future challenger launch
needs its own frozen recipe and predictions saved before outcomes; it must not
replace existing paper or shadow evidence.

## Reproduce and verify

Set `PAVLOV_BYPASS_CONFIG=1`, `OMP_NUM_THREADS=1`, and `OPENBLAS_NUM_THREADS=1`:

```text
python -m scripts.run_weather_reliability_challenger \
  --history <completed-GFS-directory> \
  --market-history <saved-weather_segment_input.json> \
  --output <separate-output-directory>
```

Only completed GFS input is accepted. The fixed spec, input and implementation
hashes, earlier errors, fitted states, matched market vectors, and scores are
preserved in an immutable replay and report. Identical inputs reuse verified
evidence; corrupt files fail closed and are never erased. The compact repository
summary includes all station comparisons; full evidence remains in reports.
Historical forecast publication and CLI label availability remain unverified.

Validation: 108 local tests passed across the new challenger, PR23, frozen paper,
verified-run shadow, market guard, backfill continuation, and checkpoint modules.
The two checkpoint tests requiring temporary directories passed after directing
their temporary files into the writable workspace. Hosted unit-test CI is
disabled; these are local checks, not hosted verification.
