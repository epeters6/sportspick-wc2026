# Verified revision and agreement challenger

This is separate, offline research. It adds no workflow, database write,
collector, paper position, account, or order path. The October 23–November 5
daytime paper model, its risk stops, PR24 shadow arms, PR26 calibration study,
and the GitHub-owned historical backfill are unchanged.

## Why this candidate exists

The current paper model's saved probabilities are materially worse than the
same-time market baseline. Better temperature means and more merged code do
not establish an edge. The new hypothesis is narrower: verified changes in
weather forecasts might help explain **some of the market's remaining error**.
Whether those changes add information after the market has priced them is
unknown. This implementation is designed to produce an explicit rejection
when they do not.

## Fixed model

The market distribution is the starting point. Four shared coefficients adjust
its log probabilities: one current-minus-previous revision feature for each
of GFS, extended HRRR and ECMWF, and one consensus-versus-market feature.
There is no fitted station, venue, outcome-bucket intercept, or choice of the
best city after looking at results.

For each exact run, a fixed 3°F Gaussian kernel translates the forecast high
into smooth bucket features. This is a feature basis, **not a fitted forecast
uncertainty or a stand-alone weather probability forecast**. The difference
between current and previous kernel masses encodes a revision. The average
current mass minus the saved market distribution encodes consensus. Every
feature is multiplied by `1 / (1 + model-temperature-standard-deviation / 3)`
to reduce adjustments when the models disagree. Agreement between correlated
weather models is not independent confirmation of correctness.

Coefficients minimize a station/date-weighted multinomial log-loss objective
with fixed ridge shrinkage and bounded coefficients. The resulting adjustment
is normalized and capped at 2.5 percentage points per bucket. Zero coefficients
reproduce market probabilities exactly, including zero-support buckets. Missing
model pairs fall back to market. No new model borrows the frozen GFS paper
model's uncertainty. The settings are fixed before scoring and are not selected
from the paper test's observed October 23–24 losses.

## Evidence and chronology

Only explicitly supplied PR24 artifact roots are read. The existing reader
checks receipt hashes, exact initialization/availability/receipt timing,
reproduces decisions from contracts and quotes, and validates official
settlements. No prices or revision features are reconstructed from current
quotes, reanalysis, or unverified historical publication times.

The research protocol fixes three windows before their outcomes:

| Stage | Target dates | Minimum support |
|---|---|---|
| Fit | October 27–November 9 | 14 dates, 100 station/dates, 10 stations |
| Validation | November 12–18 | 7 dates, 50 station/dates, 10 stations |
| Evaluation | November 21–27 | 7 dates, 50 station/dates, 10 stations |

Two intervening calendar dates separate stages. Training settlement receipts
must precede November 11 00:00 UTC, and validation settlement receipts must
precede November 20 00:00 UTC. These conservative cutoffs precede the next
stage's first decision day. Later labels cannot change the earlier fit.
All venues and snapshots for a station/date share total weight one.

**The existing PR24 collector ends November 5. It cannot supply these full
windows.** This PR does not extend that collector or authorize additional
collection. Its early records are a pilot for checking the evidence and
features. Sufficient additional prospective collection, and any compatible
extension of the frozen receipt reader, require a separate reviewed launch.
The current single ungraded decision is not usable training data. Downloading
more historical weather alone cannot manufacture matching historical quotes
or verified forecast availability.

Validation selects the adjustment only if both Brier score and log loss
improve by at least 0.001, both exploratory three-calendar-day block intervals
exclude zero, and at least 70% of validation dates improve on both scores.
Insufficient support or failed validation selects exactly the market. This is
an offline research gate, not deployment approval. Test outcomes never select
coefficients or thresholds, and even passing this short test would not prove
durable profitability. Missing-feature test events and observed outcomes with
zero market support remain visible in the full comparison rather than being
discarded to improve scores.

All new challenger scores are **retrospective replay**, even if their fitting
labels came from earlier dates. No new challenger probabilities are being
saved before outcomes. A future prospective challenger needs its own frozen
model and collection authorization.

## Reports and operation

Reports include feature coverage/rejection reasons, independent date counts,
same-row market scores, paired score differences, and separate venue quote-only
cost sensitivity at the saved quoted costs plus 1¢ and 3¢. These hypothetical
one-contract diagnostics do not represent executed fills, the paper accounts,
or verified profit. Market fallback is not evidence of beating the market.

Use the existing research Python with `PAVLOV_BYPASS_CONFIG=1`,
`OMP_NUM_THREADS=1`, and `OPENBLAS_NUM_THREADS=1`:

```text
python -m scripts.run_weather_revision_challenger freeze --output <separate-output>
python -m scripts.run_weather_revision_challenger evaluate \
  --artifact <preserved-PR24-artifact-root> --output <separate-output>
```

Repeat `--artifact` to join preserved decisions with later official settlements.
The designated output is `reports/weather_revision_challenger`. A repeated fit
reuses immutable evidence under the same training/validation identity. Changed
or corrupted evidence fails closed; do not delete files or re-freeze to evade
an error. No local importer, quota ledger, archive worker, or live model is
opened by this runner. Protocol and initial real-artifact audit accompany this
PR.

## October 25 validation

122 local tests passed: 19 new challenger cases plus the model/lead calibration,
frozen daytime paper, verified-run shadow, market guard, archive retest, full
backfill and checkpoint suites. Hosted unit CI remains disabled. Tests exercise
probability conservation, exact market fallback, the 2.5-point cap, analytical
gradients, synthetic positive/null/reversed signals, sparse support, shared
station/date weights, receipt corruption, label cutoffs, embargoes, immutable
fit reuse and independence from later test outcomes. Synthetic success only
demonstrates that the code can recover a planted signal; it is not market evidence.

The preserved real artifact from ordinary run 40214899228 reproduced one Denver
decision and six verified run receipts. All three model-pair features were
computed successfully. That October 26 target is outside this protocol's
training window, and there are zero official settlements. The result is
`awaiting_verified_revision_training_data`, with no fitted real challenger and
zero new forward predictions. The paper, verified shadow and hosted backfill
enable flags were all read back as true; the existing workflows remain active.

References: [Open-Meteo original model runs](https://open-meteo.com/en/docs/single-runs-api)
and [SciPy bounded optimization](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.minimize.html).
These support the receipt-source and numerical implementation, not a claim of
predictive advantage.
