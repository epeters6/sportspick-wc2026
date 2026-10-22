# Conditional temperature research and collection repair

The existing daytime paper model, October 23–November 5 cohort, bankrolls,
loss stops and immutable decisions are unchanged. The hosted full archive
remains the sole backfill owner. This work never starts a local importer,
changes download budgets, or promotes a research candidate.

## Separate offline candidates

Run `python -m scripts.run_weather_condition_calibration --history <completed-GFS-directory> --output <new-research-directory>`.
This command reads the completed single-run GFS cache only. It does not fetch
weather, train the hosted full archive, access Supabase, or place orders.

The immutable specification fits a regularized relationship between forecast
temperature range and temperature error, with pooled and station-specific
coefficients. Each fit uses the previous 60 calendar days and an assumed
three-target-day label lag. Current/future target labels are excluded.
Uncertainty uses signed errors from earlier out-of-sample predictions,
conditioned on forecast ranges below 10 F, 10–20 F, and at least 20 F. Sparse
groups explicitly fall back to the pooled history. Station distributions
shrink toward their range group; one-degree error bins and one-degree Gaussian
kernels preserve asymmetric tails with bounded evaluation cost.

The specification, all fitted states, prediction lineage and input hashes are
saved before evaluation. Corrupt or mismatched evidence fails closed. No
existing report, receipt, model or frozen specification is replaced.

### October 22 exploratory replay

Input: 4,153 station/dates, 21 stations, April 3–October 18. Evaluation:
608 station/dates across September 20–October 18 (29 previously inspected
dates). The range hypothesis was informed by those inspected dates. This is
not a fresh holdout and the three-day availability assumption is unverified.

| Measure | Existing reference | Conditional mean | Conditional asymmetric |
|---|---:|---:|---:|
| Daytime MAE, F | 2.3095 | 2.3313 | 2.2892 |
| Daytime CRPS, lower is better | 1.6899 | 1.6567 | 1.6015 |
| Daytime signed bias, F | +0.0388 | -0.3903 | -0.4446 |
| Nighttime MAE, F | 2.5506 | 2.5970 | 2.5063 |
| Nighttime CRPS | 1.8299 | 1.8543 | 1.7900 |
| Nighttime 90% interval coverage | 85.69% | 85.53% | 86.02% |
| Nighttime 90% cold / warm misses | 72 / 15 | 73 / 15 | 52 / 33 |
| Nighttime 90% mean width, F | 9.2098 | 9.2098 | 9.4581 |

Daytime reference is PR20 mean recentering with PR19 width; nighttime reference
is PR19. The prespecified daytime primary is the conditional mean: its overall
MAE slightly worsens despite better CRPS. Narrow-range daytime MAE improves
2.7172 to 2.2515 F, but the middle-range group worsens 2.0609 to 2.2595 F.
San Francisco daytime MAE improves 5.1994 to 4.1111 F. Report all stations and
groups rather than selecting the favorable ones after evaluation.

The prespecified nighttime primary is the asymmetric candidate. It reduces
warm bias and improves the distribution score, but total 90% misses improve
only from 87 to 85. Cold misses decrease while warm misses increase. Coverage
is still below nominal; this does **not** resolve nighttime uncertainty.
Paired seven-day block intervals for both primary candidates include no
improvement. Only five calendar blocks are available. No deployment or market
outperformance claim follows from these results.

Run identity: `64bb5705beb1a756979f998fcd2053e795ad7f98f83a271b3f584963ae40902b`.
Full local evidence is under `reports/weather_condition_calibration_20261022`
in the primary checkout. This is separate from the hosted full-history retest.

## Collection reliability

`backend/weather_collection_runtime.py` wraps the unchanged frozen v3 collector.
The entire `backend/ml/weather_learning` directory is unchanged; identical
inputs generate byte-identical forecast rows in the regression test.

- Outside all supported station decision hours, skip catalog/network discovery
  while preserving the existing hourly claim and normal zero-capture report.
- A read-only duplicate-hour preflight avoids discovery. The original atomic
  claim still protects against races. A duplicate skip never opens an old or
  absent snapshots file and never fabricates market-guard forecasts.
- Read Polymarket US climate catalogs without hydrating every catalog price.
  Preserve all contract fields, rules, station checks and partition checks.
  Actual eligible vectors still use the frozen fresh-book identity/state,
  timestamp, depth, cost and probability checks.
- Pagination is bounded and rejects truncation, malformed pages and repeated
  identifiers. Missing discovery is a visible failure, not zero opportunity.
- Preserve existing public request pacing and retry limits. A 429 stops further
  requests to that host for the invocation. A 17-minute runner deadline stops
  additional collection requests, leaving time to write collected evidence,
  guard forecasts and the completion report before the 20-minute step limit.
  Budget/provider failures remain visibly degraded even when some rows persist.
- Reject missing, stale or count-mismatched current collection files. Only the
  explicit, consistent duplicate-hour skip is exempt from reading a file.
- Preserve corrupt cached public receipts and fail visibly instead of silently
  refreshing over them; require guard inputs to match the current collection ID.

The [official US SDK](https://github.com/Polymarket/polymarket-us-python/blob/main/polymarket_us/types/events.py)
documents the `categories` filter. A saved known temperature event identifies
its category as `climate`, with a weather tag. A live public check found 97
catalog candidates and 96 rule-verified contracts across six currently listed
stations in two requests (about 1.45 seconds); one candidate failed station
verification. No order books or historical forecasts were requested by that
probe. This is catalog verification, not a completed hosted collection run.
A subsequent fresh-process check of both venues completed in 47.34 seconds,
with 456 verified Kalshi contracts and 96 verified Polymarket contracts across
40 catalog requests, zero discovery errors and zero database writes. The same
single unsupported-station candidate remained rejected. Quotes were not fetched.

Hosted verification must occur after merge on an ordinary eligible run, with
persisted snapshots and guard artifacts checked. Unit-test CI remains disabled.
