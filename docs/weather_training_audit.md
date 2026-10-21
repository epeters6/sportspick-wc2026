# Event-weighted training ablation

This offline experiment tests specific implementation weaknesses without changing
the frozen v3 trainer, deployed models, market guard, Philly study, or full-archive
retest. It reads saved local evidence only. No network requests or trading actions.

## Hypotheses specified before the comparison

1. The original classifier weights have total mass equal to the number of bucket
   rows. At fixed logistic `C`, repeating the same snapshots therefore changes
   effective regularization despite equal relative event weights. The candidate
   gives each station/date a fixed total weight of one.
2. Original feature scaling is unweighted, although classifier fitting is
   weighted. Events with more snapshots can change feature means and scales. The
   candidate uses the same weights for preprocessing and fitting.
3. Original event keys include high/low, and vectors with more buckets contribute
   more rows within an event. The candidate divides one station/date weight among
   complete vectors and then among buckets. High/low, venues, and hours share it.

The tests demonstrate that repeating identical evidence changes the original
logistic predictions but leaves the corrected fit and scaler unchanged. Distinct
new forecast inputs can still change the within-event average. Cities can share
weather shocks, so station/date totals are not independent calendar days.

## Fixed comparison

`weather_training_audit_v1` uses the unchanged v3 feature allowlist and chronological
split with label-availability embargoes. Every variant receives the exact same
officially labelled v3 vectors; original bootstrap v2 rows are excluded from all
variants. Controls are a refit with original preprocessing/weights and a refit
changing only preprocessing weights. These do not recreate the deployed mixed
v2/v3 model or its calibration scoring.

Corrected logistic candidates use `C = 0.1, 1, 10`, fixed before evaluation. Each
fits on training dates, calibrates temperature on calibration dates, and is scored
with equal station/date weight throughout. Candidate selection uses calibration
log loss only. Calibration eligibility requires seven dates, 75 station/date
events, and at least 0.001 improvement in both Brier and log loss over the matched
market baseline. There is **no automatic promotion**, even if this gate passes.
No thresholds are relaxed after examining results.

Test dates are displayed only after model selection. This is an exploratory replay
of already inspected historical evidence, not fresh forward validation. Saved
original logistic/tree forecasts are also scored on precisely the same test rows.
Matching their score does not show improved trading returns.

The experiment intentionally retains binary bucket fitting and subsequent vector
normalization to isolate these weighting/preprocessing changes. Their objective
mismatch is unresolved. The separate market-correction experiment already tests
coherent market-relative vector fitting and must not be silently replaced.

## Reproduction and evidence

Run `python -m scripts.run_weather_training_audit --input <saved-history.json>
--output <new-directory> --as-of <evidence-cutoff>`. The exact spec is persisted
before fitting. Report identity binds the input bytes, implementation, frozen v3
helpers, cutoff, and sklearn version. Existing mismatched reports fail closed.
Candidate coefficients are research parameters with actual creation time; no
past forward forecasts are generated or deployment files overwritten.

The underlying library supports weighted scaling through
[StandardScaler sample_weight](https://scikit-learn.org/stable/modules/generated/sklearn.preprocessing.StandardScaler.html).
The regularization parameter is documented in
[LogisticRegression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html).

Archive temperature corrections still need the complete fixed backfill. A stronger
candidate then needs forecasts recorded before outcomes and adequate cost/fill
evidence. This experiment cannot establish profitability or live readiness.

## October 21 result

The fixed comparison used 164 training station/date events across nine dates,
52 calibration events across three dates (September 30–October 2), and 53 test
events across three dates (October 4, 19, 20). The outage gap remains in the split.
The selected corrected candidate was `C=10`, chosen on calibration log loss.

| Method | Calibration Brier | Calibration log loss | Test Brier | Test log loss |
| --- | ---: | ---: | ---: | ---: |
| Matched market | 0.437064 | 0.823856 | 0.376999 | 0.941313 |
| Original-method logistic refit | 0.441159 | 0.817799 | 0.371024 | 0.741618 |
| Weighted scaling only | 0.441265 | 0.817875 | 0.370568 | 0.740321 |
| Corrected, C=0.1 | 0.447397 | 0.836470 | 0.364493 | 0.714372 |
| Corrected, C=1 | 0.443948 | 0.824073 | 0.369085 | 0.724255 |
| Corrected, C=10 — selected on calibration | 0.441128 | 0.816305 | 0.371662 | 0.744988 |

The selected corrected candidate modestly beats the market on the three test
dates, but is slightly worse than the original-method refit on both test scores.
Its calibration Brier also remains worse than market, and calibration coverage
is below both fixed minima. It is **not qualified or promoted**. C=0.1's better
displayed test result is not grounds to override calibration selection. The
weighting fixes remove demonstrated training instability; this experiment does
not show that they reliably improve predictive performance.

Original saved forward logistic and tree forecasts, restricted to these same
test rows, have Brier 0.371530 and 0.394777 respectively. This restricted three-date
subset is not interchangeable with the earlier 17-date forward audit, on which
both models lagged market. Refitted candidates are retrospective research.

Evidence cutoff: `2026-10-21T21:54:58.117940+00:00`; sklearn 1.9.1.
Run ID: `59cf4ccb8447a679f075de23964deb51ad5b1c0508947ecdc9c79cf1bf9e7c7c`.
Report hash: `b5d3eb6b886bb39d77cc2e9e71fac01f6e0e6590c29b3b819452da2a4adb5b64`.
Input SHA-256: `1732727ef50a05b78cce61b3acf2e37d083c4cb45f8941f00d52ee0bc506bfc5`.

Validation: 62 local tests passed across the new ablation, market guard, frozen
learning, market correction and archive retest suites. Hosted unit CI remains
disabled. Weighted sklearn scaling emitted a roundoff warning on constant
`model_count` (variance about -2.1e-39); its constant-feature handling produced
finite positive scales, and all model probabilities/scores were finite.
Frozen v3 and full-archive retest implementation hashes remain unchanged.
