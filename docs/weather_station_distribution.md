# Station temperature bias and uncertainty, separate GFS experiment

The completed GFS-only cache contains 4,153 officially labelled station-days
across all 21 stations, April 3–October 18. It can support physical-weather
calibration while the multi-model decision archive continues on GitHub. This
experiment does not modify that archive, its fixed retest, or any deployed model.

## Fixed specification before evaluation

Use the previous calendar date's original 00 UTC GFS run and exact local
standard-day extrema. Train mean corrections on April 3–August 29; estimate
uncertainty on August 31–September 18; report September 20–October 18 separately.
August 30 and September 19 remain unused buffers. These are retrospective
partitions: archive public availability and historical label publication times
are unverified, and some of these outcomes have been inspected previously.

Fit high and low separately. Start with the pooled training mean error. Estimate
each station's error, shrinking toward the pooled mean with a fixed 30-day prior
to temper small samples. Fit residual mean squares only on later calibration
dates, around these frozen mean predictions. Shrink station residual mean squares
toward the pooled residual mean square using a fixed 30-day prior; use a 1.5°F
sigma floor. Residual means are not subtracted during variance fitting, so any
remaining calibration bias is included in the predictive error estimate.

Compare all four prespecified controls/candidates on identical test rows:

- Raw GFS mean with its pooled calibration error width.
- Pooled mean correction with its pooled error width.
- Station mean correction with its pooled error width.
- Station mean correction with station-specific, shrunk error widths (primary).

The raw GFS forecast is deterministic. Its distribution here is an explicit
calibrated control, not a probability forecast supplied by the weather provider.
No candidate, station, date, threshold or distribution family is selected using
the displayed test results. There is no automatic promotion.

Report MAE, RMSE, signed error, Gaussian negative log likelihood, CRPS, and
50/80/90/95% interval coverage together with interval width. Wider intervals can
raise coverage without improving a proper probability score. CRPS assesses the
entire distribution; lowering temperature MAE alone is insufficient. Paired
bootstrap intervals resample seven-calendar-day blocks with all cities together.
There are few test blocks; these intervals are exploratory, not proof of an edge.

Bias and spread calibration are distinct operations, as described in
[ECMWF's post-processing course](https://events.ecmwf.int/event/405/contributions/4489/attachments/2522/4334/tc2024_calibration.pdf).
The shrinkage constants and split above are this experiment's fixed choices,
not a claim of optimal settings or a direct implementation of ECMWF's models.

## Run and evidence

Run `python -m scripts.run_weather_station_distribution --history
<completed-GFS-directory> --output <new-output-directory>`.

The command reads existing files only. It verifies the dataset hash, station/run/
standard-day identity, both temperature extrema, provenance and ineligible-for-
production flags. It fails closed for duplicate events, mismatches, insufficient
station coverage, future receipts, and conflicting immutable reports. The spec
is written before fitting. The model artifact is written before test scoring.
Creation time is actual run time, never backdated to a historical split.

The resulting parameters and diagnostics do not create past forward forecasts,
place orders, or establish market outperformance. Transfer to the actual mixture
of GFS/HRRR/ECMWF and decision hours requires the separate complete-archive test
and subsequent genuinely forward evaluation.

## October 21 comparison

The immutable run used 3,104 training events over 148 available dates, 399
calibration events over 19 dates, and 608 test events over 29 dates. All 21
stations are represented in each partition. There are 25 missing station/dates
in training, none in calibration, and one in test; buffers are excluded. High
and low are two measurements of each event, not twice as many samples.

| Metric / method | MAE °F | RMSE °F | CRPS °F | Gaussian NLL | 90% coverage | 90% width °F |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| High, raw GFS + calibrated pooled width | 2.506 | 3.444 | 1.844 | 2.656 | 92.1% | 11.58 |
| High, pooled bias | 2.671 | 3.523 | 1.916 | 2.679 | 91.1% | 11.33 |
| High, station bias + pooled width | 2.536 | 3.237 | 1.809 | 2.595 | 91.4% | 11.05 |
| High, station bias + station width | 2.536 | 3.237 | 1.803 | 2.581 | 92.3% | 11.03 |
| Low, raw GFS + calibrated pooled width | 3.374 | 4.351 | 2.443 | 2.948 | 81.9% | 11.44 |
| Low, pooled bias | 2.969 | 3.868 | 2.150 | 2.813 | 83.7% | 10.53 |
| Low, station bias + pooled width | 2.824 | 3.685 | 2.063 | 2.845 | 79.4% | 8.87 |
| Low, station bias + station width | 2.824 | 3.685 | 2.064 | 2.866 | 78.5% | 8.84 |

Nighttime MAE improves 16.3% and CRPS 15.5% against the raw-mean control. Daytime
MAE is 1.2% worse, while RMSE and CRPS improve. These are physical forecast
results, not market or profit results.

The uncertainty component is **not resolved**: for lows, station-specific widths
slightly worsen CRPS/NLL relative to station bias with pooled widths. Their
nominal 90% intervals cover only 78.5%, and the later-month mean forecast remains
1.84°F too warm. Calibration widths from late August/early September do not
adequately describe the later errors. Seasonal/regime changes and tail shape
are hypotheses for further work, not established causes from this comparison.
We do not widen intervals on these test outcomes or promote the candidate.

Against pooled bias, station corrections reduce low MAE by 0.145°F; its paired
seven-day-block bootstrap interval is [-0.206, -0.054]°F. The corresponding CRPS
interval includes zero, as do daytime MAE/CRPS intervals against pooled bias.
Only five calendar blocks underlie these exploratory intervals. Station width
adjustment alone has no clear low-CRPS improvement (delta +0.00058°F, interval
[-0.00393, +0.00505]°F). No city is selected on these results.

Run ID: `a65b50da1a7254ef4547377590fad5e27d09f681ad2765d52246f4f366d2f00a`.
Report hash: `ecd8142fc47b043cc6b641733b4b4f2eb3c1a2ea21da0e7c00a98bff82f76f4d`.
Model ID: `a460265d0d73564da04caa80931df2c30596d22c331cb38d1ec484eb8344f030`.
Input SHA-256: `a932f1da8fa62cbaf04fcf3e314e6b01d3472aa031b73c4552b35132f59ed8b1`.

Validation: 46 local tests passed across station distributions, historical
calibration, probability screening, archive retest, and training ablation.
The pre-existing sklearn constant-feature roundoff warning in the ablation
suite remains investigated and unchanged. Hosted unit CI remains disabled.
Frozen v3 and full-archive retest implementation hashes are unchanged.
