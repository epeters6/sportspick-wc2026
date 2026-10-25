# Model-specific lead calibration and fixed fresh-run evaluation

This is a separate offline research path. It changes no workflow, database,
account, frozen paper model, verified-run shadow arm, historical archive,
provider quota, or loss stop. PR25's mixed candidate remains offline. The
hosted backfill remains the only archive owner.

## What is fixed before scoring

`protocol.json` records the creation time, specification and implementation
hash. It was frozen October 25 before the evaluation's first decision day.
The future comparison includes October 27–November 5 targets, all 21 stations,
and only PR24 decisions saved after protocol creation. This is a subset for a
separate evaluation; it does not change or extend PR24 or the paper cohort.
The previously inspected October 26 Denver forecast is excluded from scoring.

The primary saved-forward comparison is PR24's fresh GFS arm against the same
market quotes. Its frozen-recipe arm is a secondary comparison. The existing
guard still uses market weight zero. Forecast revisions below/at least 2°F are
fixed diagnostic strata, not reasons to select stations or change probabilities.
No model, threshold, station, or subgroup is chosen from these outcomes.

## Separate calibration for each model and forecast lead

The fixed archive is April 3–October 19, all 21 stations. Fit mean corrections
on April 3–August 27, calibrate uncertainty on August 31–September 16, and
evaluate September 20–October 19. The gaps exceed the assumed label-maturity
buffer. Final historical publication/label availability remains unverified;
these are exploratory archive partitions, not a permanent unseen holdout.

GFS, extended HRRR and ECMWF receive separate high/low calibration. Forecast
lead is hours from exact run initialization to the start of the station's
standard day, with fixed boundaries 18, 30 and 42 hours. It is distinct from
run age at the decision. A new model never inherits the midnight-GFS correction.

Within each model/lead cell, station mean errors shrink toward that cell's
pool with a 30-event prior. Station-specific correction needs 14 station/date
events. Uncertainty uses later calibration RMS errors with the same shrinkage
and a fixed 0.75°F floor. Pools require 60 station/date events and seven dates.
Unsupported cells stay unavailable; they do not borrow another model's width.
Identical station/date/model/run observations appearing at multiple local
slots are deduplicated, and remaining snapshots share station/date weight.

The primary offline candidate is an equal mixture of the three separately
calibrated distributions. Each model is also reported individually, alongside
raw-model temperature errors. There is no winner selection or fitted revision
coefficient. Metrics include MAE, bias, CRPS, and 50/80/90/95% interval coverage
and width. Model comparisons use the same rows; incomplete model panels and
unsupported cells remain explicit coverage gaps.

## Market comparison and evidence integrity

The reader uses explicitly supplied, preserved PR24 artifact directories. It
verifies compressed forecast receipts, reconstructs every shadow decision from
the exact model, contracts, quotes and forecast runs, and validates official
settlement receipts before recomputing scores. Cross-platform floating-point
reconstruction tolerance is 1e-12. Conflicting duplicates, missing decisions,
bad hashes and stale/future receipt timing fail closed. No network request or
database write is implemented here.

Brier and log loss compare each arm with market probabilities on exactly its
matched rows. Venues share station/date weight. Three-calendar-day bootstrap
blocks keep stations on the same dates together. Intervals require seven dates
and fourteen station/date events, and remain exploratory in this short cohort.
The primary cohort has at most ten dates and cannot establish durable profit.

Quote-only selection chooses at most one bucket per venue/station/date with a
5% edge after the saved quoted costs and available depth. Reports keep each
venue separate, show missing cost/depth evidence, and include additional 1¢
and 3¢ slippage. Reliability bins show claimed odds and observed win rates for
selected contracts. These are hypothetical one-contract sensitivities, not
fills, bankroll simulations, or returns from the frozen paper test. No risk
limits from the paper test are changed or simulated here.

When a new fitted calibration is supplied, its predictions on saved PR24
quotes are **retrospective replay**, even when training labels predate those
quotes. They are kept separate from PR24's saved forward arms. This tool never
creates or backdates forward predictions, and never promotes a model. A new
forward candidate would need a separate frozen launch and evidence collection.

## Completion gate and operation

Use only the final hosted checkpoint after the existing monitor downloads and
verifies it. The fit command requires the original successful retest's
`completion.json`, report hash, final archive plan, full request count, dataset
hash, and all 21 stations in each partition. It never opens quota ledgers or
starts an importer. Missing completion returns a waiting status. Immutable
fit evidence is reused without retraining; corrupt evidence is never erased.

Set `PAVLOV_BYPASS_CONFIG=1`, `OMP_NUM_THREADS=1`, and `OPENBLAS_NUM_THREADS=1`.
The output directory must contain the exact frozen `protocol.json`:

```text
python -m scripts.run_weather_model_lead_calibration fit \
  --archive <verified-final-checkpoint>/archive \
  --completion-dir <verified-final-checkpoint>/retest \
  --output <separate-model-lead-output>

python -m scripts.run_weather_model_lead_calibration evaluate \
  --artifact <preserved-shadow-artifact-root> \
  --output <separate-model-lead-output>
```

Repeat `--artifact` for retained runs needed to join decisions and settlements.
Optionally supply `--fit-evidence <immutable-fit-file>` for explicitly
retrospective calibrated replay. Protocol reuse rejects any changed recipe or
implementation. Do not freeze again to work around an error.

## October 25 validation and current status

92 local tests passed, including 15 new tests plus frozen daytime paper,
verified-run shadow, original archive retest, full backfill continuation and
checkpoint suites. Two checkpoint tests required temporary files inside the
writable workspace and then passed. Hosted unit-test CI remains disabled.
Tests cover model/lead separation, duplicate-run weights, future-outcome
perturbations, sparse support, interval math, completion and hash gates,
immutable reuse, decision reconstruction, official settlements, matched-market
scoring, separate venue costs and date-based confidence requirements.

The real first hosted shadow artifact, run 40214899228, reproduced successfully:
one Denver decision and six forecast receipts. Its October 26 target is outside
the newly fixed comparison, so the evaluator reports zero included decisions
and unknown scores. The full archive is incomplete; no real multi-model fit,
new calibrated forward probability, improved accuracy, or profit is claimed.

Provider references: [single original runs](https://open-meteo.com/en/docs/single-runs-api)
and [initialization versus availability](https://open-meteo.com/en/docs/model-updates).
The latter documents the extra propagation margin; PR24's existing verified
receipt rules remain unchanged.
