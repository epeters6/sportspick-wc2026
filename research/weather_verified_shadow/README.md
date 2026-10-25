# Verified-run shadow comparison

This separate prospective experiment asks whether a fresher GFS initialization
improves next-day high probability forecasts relative to the frozen daytime
recipe and the same contemporaneous market. It does not change or trade the
existing daytime paper account. There is no model fitting, historical importer,
nighttime promotion, or account/order access in this runner.

## Fixed comparison

The parent is daytime model `7b7d1c759cf762af36dec1d9789ac7666220631e2b6ecb4fd509785da3da6c18`.
For all 21 stations, the first valid local 08/11/14/17 slot for each venue and
next-day target is captured prospectively, through November 5, 2026. No past
decisions are reconstructed. Starting later means missing earlier cohort dates.

Each complete contract ladder uses the same fresh quotes and decision time for:

* Normalized two-sided market prices.
* Frozen daytime temperature correction and width on its original 00 UTC run.
* The same correction and width on the latest verified GFS run: an **unvalidated
  transfer experiment**, not an assertion that corrections transfer across runs.
* The conservative market fallback, with model weight fixed at zero. Matching
  the market is not beating it.

The reference arm is recalculated at the shadow decision time; it is not the
actual earlier paper decision or paper position. Only paired official outcomes
are scored. Multiclass Brier and log loss average venues within station/date,
then average station/dates. Repeated buckets and venues are not independent
samples. Quoted ask, fees, depth and slippage support net-edge diagnostics only;
the experiment opens no positions and reports no realized profit.
Paired Brier differences include an exploratory date-block bootstrap interval
only after seven officially graded target dates; whole dates are resampled so
correlated cities are kept together. The interval is not a promotion rule.

## Forecast timing and revisions

Model initialization, provider metadata availability, and our receipt time are
different facts. The provider documents a ten-minute propagation margin after
`last_run_availability_time`. Metadata selects a plausible initialization; a
successful exact single-run response received before the actual decision is
required to establish that its values were available by then. Neither proves
the exact original publication time or historical availability. Metadata
modification time is deliberately not treated as publication.
For `gfs_global` temperature, metadata is read from the **primary gfs013**
domain, not the supplemental gfs025 domain. The provider's
[domain mapping](https://github.com/open-meteo/open-meteo/blob/main/Sources/App/Controllers/ForecastapiController.swift)
distinguishes these readers; their advertised update times can differ.

Each receipt binds endpoint, exact model/run/location request, response hash and
actual receipt time. A full 24-hour **standard-day** temperature series is
required, including on DST transitions. Hourly extrema are proxies for official
daily extrema. Grid/elevation/unit mismatches, future receipts and incomplete
days are rejected. Revision features compare distinct six-hour-separated runs
on the same grid and target. Two snapshots of one initialization do not count as
a forecast revision; the older run may legitimately be fetched second.

GFS revisions are required for a comparison. Extended HRRR and ECMWF current and
previous runs are optional research covariates with explicit rejection reasons.
Short-horizon HRRR cycles are excluded. Missing auxiliary coverage is not filled
with today's forecast or treated as zero disagreement. There are no fitted
revision coefficients: these prospective receipts are the evidence needed to
test such coefficients later without inventing historical availability.

Sources: [provider timing semantics](https://open-meteo.com/en/docs/model-updates),
[exact-run endpoint](https://open-meteo.com/en/docs/single-runs-api),
[GFS cycles](https://www.nco.ncep.noaa.gov/pmb/products/gfs/),
[extended HRRR cycles](https://rapidrefresh.noaa.gov/hrrr/).

## Isolated persistence and operation

Only `weather_verified_run_shadow_v1:*` and
`weather_verified_run_shadow_v1_latest` app-settings keys are written. Receipt
bodies are compressed and deduplicated by exact request; decisions retain their
receipt identities. Conflicting/missing state or corrupt evidence fails closed.
Conditional state writes and an immutable outbox recover interruptions without
overwriting old evidence or resetting an account.

New prospective forecast requests have a separate fixed ceiling of 150 per
invocation, UTC day **and** rolling 24 hours, with 1.3-second pacing. Each attempt
is durably reserved before network access, including failed/unknown attempts.
An interrupted or failed request leaves a 24-hour provider cooldown. Metadata
reads are unmetered per provider documentation. This runner never reads or
modifies the backfill's quota ledger or raises its 6,000-unit allowance.

After review and merge, explicitly initialize once on main:

```powershell
python -m scripts.run_weather_verified_shadow --initialize
```

Then set `WEATHER_VERIFIED_RUN_SHADOW_ENABLED=true`. Initialization is separate
from collection and ignores existing state, never resetting it. The default
workflow gate is off. The independent job uses ordinary Weather Paper Research
triggers, does not need other jobs to succeed and does not dispatch late runs.
Collection ends at the fixed cohort endpoint; grading continues in bounded
batches of 12 events with six-hour label retries. Artifact retention is 90 days;
immutable database receipts and decisions are retained beyond artifact expiry.

Before calling collection verified, inspect actual persisted decisions, receipt
hashes and subsequent official settlement records. A green job or a persisted
empty report is not evidence of forward collection or market outperformance.
Monitor compact `weather_verified_run_shadow_v1_latest` and preserve failures
and notice fingerprints separately from the frozen paper and backfill state.

## Saved public-data validation

`probe_20261023` retains the original October 23 six-response probe and quota
reservations. Review then corrected GFS metadata selection from supplemental
gfs025 to primary gfs013. The October 24 `primary_gfs_summary.json` and two new
receipts supersede **GFS run selection only** in the first probe; original files
are preserved. Total test usage was eight location units across the two days.
All three model pairs revalidate from saved receipts without network access.
These are timing/identity checks, not paper decisions, historical publication
proof, realized profit, or evidence of predictive improvement.
