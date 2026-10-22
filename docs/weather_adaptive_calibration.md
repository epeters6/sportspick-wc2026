# Rolling station temperature and uncertainty experiment

PR #18's static station corrections left nighttime forecasts too warm and their
90% ranges covered only 78.5% of later observations. Daytime average absolute
error was slightly worse than raw GFS. This separate experiment tests whether
recent bias and earlier out-of-sample prediction errors help. Those results
motivate the design, so the reused months cannot be called a fresh holdout.

## Specification fixed before scoring

Use the completed April 3–October 18 GFS cache and all 21 stations. Keep the
same September 20–October 18 comparison dates, with one previous-day 00 UTC run
and exact local standard-day high/low per station/date. Fit high/low separately.

- At target date D, only labels for target dates through D minus three days
  may enter a fit. This is an assumed maturity delay, not verified historical
  publication timing. All artifacts retain actual creation time and remain
  ineligible for production training.
- Refit station mean errors using the last 45 calendar days ending at that
  cutoff. Shrink toward the recent pooled error with a 15-station-day prior;
  require at least 30 dates per station. No hyperparameter search.
- Replay earlier mean predictions in date order. Each excludes its own label
  and future labels. Estimate widths from the last 60 calendar days of these
  predictions' now-assumed-mature squared errors, with at least 30 per station.
  Shrink toward the pooled mean square using a 30-day prior and keep a 1.5°F
  sigma floor. Do not subtract remaining residual bias from these errors.
- Compare raw GFS and the exact PR #18 static candidate with rolling means
  using PR #18 widths, then rolling means with the new error widths (primary).
  Show all variants and stations; no station selection or automatic promotion.

The three-day lag, windows and priors are fixed research choices, not optimized
values or a guarantee that older labels were actually available. Earlier
comparison-period labels can update later predictions: this is a rolling replay,
not a model frozen before the entire comparison period. PR #18's controls keep
their original partitions and unused buffers; the new replay uses every earlier
eligible date, including those buffers, under its separately saved specification.

Report MAE, RMSE, signed bias, Gaussian CRPS/NLL and 50/80/90/95% coverage with
width. Higher coverage alone is insufficient because indiscriminate widening
can degrade proper scores. Compare paired seven-day calendar blocks with all
stations together, while acknowledging only five blocks and reused data. The
bootstrap does not rerun fitting and is not a model-selection guarantee.

Run `python -m scripts.run_weather_adaptive_calibration --history
<completed-GFS-directory> --output <new-output-directory>`.

The command reads cached files only. It saves the specification before fitting,
then every simulated comparison prediction and its fitted state before scoring.
Source hashes, receipts, original-run identity, standard days, coverage and
immutable artifact identities fail closed. It creates no operational forecasts,
does not consume provider quota, and does not change the cloud backfill, its
fixed retest, the deployed v3 model, or the frozen Philly study.

Daily lows/highs are proxies for nighttime/daytime errors here. Transfer to
actual decision hours and the GFS/HRRR/ECMWF mixture requires the full archive
and subsequent forward validation. Temperature scores alone do not measure
market outperformance or executable profit.

## October 22 UTC results

The run used 4,153 source station/dates, with 26 missing events in the fixed
199-day source interval. The comparison contains 608 station/dates across all
21 stations and 29 dates; KPHX September 27 is the one missing comparison event.
The raw and static controls exactly reproduce PR #18's saved scores.

| Metric / method | MAE °F | RMSE °F | CRPS °F | Gaussian NLL | 90% coverage | 90% width °F |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| High, raw GFS + calibrated width | 2.506 | 3.444 | 1.844 | 2.656 | 92.1% | 11.58 |
| High, PR #18 static | 2.536 | 3.237 | 1.803 | 2.581 | 92.3% | 11.03 |
| High, rolling mean + static width | 2.487 | 3.212 | 1.773 | 2.568 | 92.3% | 11.03 |
| High, rolling mean + earlier prediction error width | 2.487 | 3.212 | 1.763 | 2.550 | 91.3% | 10.83 |
| Low, raw GFS + calibrated width | 3.374 | 4.351 | 2.443 | 2.948 | 81.9% | 11.44 |
| Low, PR #18 static | 2.824 | 3.685 | 2.064 | 2.866 | 78.5% | 8.84 |
| Low, rolling mean + static width | 2.551 | 3.301 | 1.838 | 2.673 | 83.2% | 8.84 |
| Low, rolling mean + earlier prediction error width | 2.551 | 3.301 | 1.830 | 2.638 | 85.7% | 9.21 |

Relative to PR #18, nighttime MAE improves 9.7% and CRPS 11.3%. The warm
signed bias falls from +1.84°F to +0.88°F. Most of the coverage improvement
comes from the mean correction; changing widths adds about 2.5 percentage
points. Widths grow by about 4.2%, while CRPS and NLL both improve. However,
85.7% coverage still undershoots the nominal 90%; nighttime uncertainty is
improved, not fully calibrated.

Daytime MAE improves 1.9% and CRPS 2.2% relative to PR #18, but the signed
cold bias worsens from -0.75°F to -1.02°F. The MAE gain against raw GFS is
only 0.7%. The paired MAE interval versus PR #18 is [-0.260, +0.159]°F and
CRPS interval [-0.176, +0.101]°F, so neither daytime improvement is clear
across these few calendar blocks. MAE improves in 11/21 stations for highs
and 16/21 for lows; all stations remain in the experiment.

Nighttime paired deltas against PR #18 are -0.273°F MAE (interval
[-0.419, -0.158]) and -0.234°F CRPS ([-0.369, -0.136]). Width adjustment
alone lowers CRPS by 0.0080°F for lows and 0.0095°F for highs. These are
exploratory block intervals on already inspected data, not confirmation from
fresh outcomes. No parameters were retuned after seeing this run.

The candidate remains offline. The next evidence needed is a separate check
on the completed multi-model archive and genuinely forward, matched market
forecasts. This PR does not change the already fixed full-backfill retest.

Run ID: `f7d8215b33394dfb884777bc8c2c547890a985d0e3f9e87f3d3559dda7b77f1c`.
Report hash: `2b9b9046faf8b75c801fccfd79c9bfc38c9b95abd2908d6267a7b14304c5d783`.
Replay artifact ID: `eda8ddd61e080dad11e3ba74c48f03981ead34a4786996c405baac6649f442b8`.
Specification hash: `d22cc7f600a8b0289292bbe5764fff9da9830658d470850427a03274c85ed09a`.
Implementation hash: `e6dceda3e159802d3b5c6760b17ecd7b15bf1d9b6d62b21f384271d6d6dc6a78`.
Input SHA-256: `a932f1da8fa62cbaf04fcf3e314e6b01d3472aa031b73c4552b35132f59ed8b1`.

Validation: 54 local tests passed (8 new replay tests and 46 existing research
and archive checks). They cover future-label isolation, assumed label maturity,
out-of-sample residual widths, shrinkage, floors, duplicate rejection, insufficient
history, immutable evidence and saving predictions before scoring. The existing
sklearn constant-feature roundoff warning remains confined to the unchanged
training-ablation suite. Hosted unit CI remains disabled.
