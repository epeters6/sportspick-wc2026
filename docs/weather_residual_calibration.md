# Remaining bias and asymmetric forecast uncertainty

PR #19 reduced nighttime error, but its 90% intervals covered 85.7% of later
observations. Daytime forecasts remained 1.02°F too cold on average. This new,
separate experiment tests an additional correction based on recent signed
prediction errors, and an error distribution that need not have symmetric tails.

## Fixed specification before scoring

Use the same completed GFS cache, all 21 stations, and September 20–October 18
comparison dates. Retain PR #19's exact rolling mean and width as the comparator.
Construct earlier observed-minus-predicted errors from its rolling mean models;
each of those mean models excluded its own target label. At target D, only
errors with target dates through D minus three days may be used. The lag is an
assumption: actual historical publication times remain unverified.

Use the preceding 30 calendar days ending at that cutoff, with at least 21
dates per station. Combine a station's empirical residual distribution with
the pooled distribution using a fixed 15-station-day prior. An own-station
residual receives weight `(1 + 15/N)/(n + 15)` and another station's residual
receives `(15/N)/(n + 15)`, where n and N are local and pooled sample counts.
These weights sum to one; repeated station/date records are rejected.

Place a Gaussian kernel of fixed 1°F standard deviation at each signed residual.
Translate that distribution by the current PR #19 mean. Keeping the residual
mean corrects a remaining warm/cold bias; the mixture allows asymmetric tails.
Use the distribution mean for point-error comparisons in every variant.

Prespecify and display every variant:

- Raw GFS with the original calibrated pooled width.
- Exact PR #19 mean and width.
- Residual mean correction alone, keeping PR #19 width.
- A Gaussian with the residual mixture's exact mean and variance.
- The full residual kernel mixture (primary).

This separates mean correction, spread changes and distribution shape. No
parameter search, post-result retuning, city selection or automatic promotion.
The raw and PR #19 controls must reproduce their saved scores. All models and
comparison predictions are saved before scoring and retain actual creation
time, source hashes, parent-model links and explicit retrospective flags.

Score MAE, RMSE, signed bias, CRPS, negative log density, and central
50/80/90/95% interval coverage, width, interval score and each missed tail.
Mixture CRPS uses the exact identity `E|X-y| - E|X-X'|/2`, with the Gaussian
absolute-moment formula; log density uses log-sum-exp, and quantiles invert the
mixture CDF. The formula is consistent with the Gaussian-mixture treatment in
[Williams, Ferro and Kwasniok's post-processing comparison](https://empslocal.ex.ac.uk/people/staff/ferro/Publications/williams2012.pdf).
The fixed lag, windows and priors are our research choices, not claims of
optimality or guaranteed coverage from that paper.

The evaluation reuses already inspected months and lets earlier evaluation
labels train later predictions. It is exploratory rolling replay, not an
untouched holdout. Seven-day paired blocks keep stations together but give only
five blocks and exclude model-selection and fitting uncertainty. Daily highs
and lows are two measurements of each station/date, not independent samples.

Run `python -m scripts.run_weather_residual_calibration --history
<completed-GFS-directory> --output <new-output-directory>`.

The command reads only local cached files. It does not change the deployed
model, original experiments, GitHub backfill or its fixed retest. There is no
provider download, market-price reconstruction, order placement or spending.
Physical temperature gains must still transfer to actual decision hours and
matched forward market forecasts before any market-edge claim is justified.

## October 22 UTC comparison

Source coverage is 4,153 station/dates with 26 missing events across the fixed
199-day interval. The comparison retains 608 station/dates across 21 stations
and 29 dates; KPHX September 27 is the one missing comparison event. The raw
and PR #19 controls reproduce their saved metrics within 1e-12.

| Metric / method | MAE °F | Bias °F | RMSE °F | CRPS °F | NLL | 90% coverage | 90% width °F |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| High, raw GFS + calibrated width | 2.506 | +0.044 | 3.444 | 1.844 | 2.656 | 92.1% | 11.58 |
| High, PR #19 | 2.487 | -1.022 | 3.212 | 1.763 | 2.550 | 91.3% | 10.83 |
| High, residual mean only | 2.309 | +0.039 | 3.145 | 1.690 | 2.512 | 92.4% | 10.83 |
| High, residual Gaussian | 2.309 | +0.039 | 3.145 | 1.691 | 2.514 | 91.4% | 10.58 |
| High, residual kernel (primary) | 2.309 | +0.039 | 3.145 | 1.687 | 2.531 | 90.6% | 10.65 |
| Low, raw GFS + calibrated width | 3.374 | +2.709 | 4.351 | 2.443 | 2.948 | 81.9% | 11.44 |
| Low, PR #19 | 2.551 | +0.882 | 3.301 | 1.830 | 2.638 | 85.7% | 9.21 |
| Low, residual mean only | 2.543 | +0.432 | 3.286 | 1.823 | 2.629 | 85.2% | 9.21 |
| Low, residual Gaussian | 2.543 | +0.432 | 3.286 | 1.827 | 2.633 | 84.4% | 9.42 |
| Low, residual kernel (primary) | 2.543 | +0.432 | 3.286 | 1.833 | 2.659 | 84.9% | 9.45 |

Daytime recentering removes nearly all aggregate cold bias in this replay and
reduces MAE 7.1% relative to PR #19. The primary distribution reduces CRPS
4.3%; MAE improves in 15/21 stations and CRPS in 14/21. The paired MAE delta
is -0.178°F (seven-day-block interval [-0.364, -0.063]) and CRPS delta is
-0.076°F ([-0.169, -0.015]). These are still exploratory intervals from five
blocks on reused history. The extra mixture shape has no clear advantage over
its Gaussian moment control: its CRPS delta interval includes zero and NLL is
worse. The useful daytime finding is primarily the mean correction.

**The proposed nighttime uncertainty change fails this comparison.** Its
nominal 90% coverage falls from 85.7% to 84.9%, despite slightly wider ranges.
CRPS worsens 0.15% and NLL also worsens. Point MAE changes only 0.3%, with a
paired interval spanning zero. Only 8/21 stations improve CRPS. The 90% ranges
miss 63 observations below their lower bound (10.4%) and 29 above their upper
bound (4.8%): the colder tail remains underrepresented. Shape flexibility alone
has not resolved the failure, and the nighttime candidate is not recommended
for promotion. No thresholds or bandwidths were changed after seeing this.

Neither candidate is deployed. Preserve the full negative nighttime result and
all controls, and require fresh evidence for the daytime correction. Repeated
changes judged on these same months risk fitting the research process to the
comparison set. A separate test using the completed archive's additional
forecast inputs is more informative than repeatedly widening this test's bands.
The original full-archive retest remains unchanged.

Run ID: `7d87eddbef7c0c62e97c03c7c75279742bfaafa4ebb48d97ba55295b48c4353a`.
Report hash: `696eee5ab804a431e2c0deb00a07cfef26f591e1c0edd92f07ba01a281cbe6e6`.
Replay artifact ID: `674d7c560214b1c147adfefd38eab259766e1c8baa4f59c64424dfa986f75bb2`.
Specification hash: `9bb69c5d397039f8a29596425b6c817f0dd888e2f7ca1d06b9d6819a4fb6c1ad`.
Implementation hash: `2075cb2a90f6050d1bfa487a7ac5833c365eec28438c59a3a263a811363c8a2b`.
Input SHA-256: `a932f1da8fa62cbaf04fcf3e314e6b01d3472aa031b73c4552b35132f59ed8b1`.

Validation: 65 local tests passed (11 new cases, 54 existing). New tests check
weights and moments, mixture/Gaussian equivalence, CRPS against independent CDF
integration, asymmetric quantiles, stable tail log scores, invalid inputs,
label maturity, future-label isolation, saved evidence before scoring and
tamper rejection. Frozen v3, PR #18, PR #19, the full-archive retest and the
Philly plan retain their implementation/file hashes. Hosted unit CI remains
disabled; the existing sklearn roundoff warning stays confined to the unchanged
training-ablation suite.
