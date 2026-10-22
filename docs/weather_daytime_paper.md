# Separate daytime forward paper experiment

The user authorized testing the improved daytime candidate while the multi-model
archive continues. `weather_daytime_paper_v1` tests daily **highs** at all 21
registry stations, for target dates **October 23–November 5, 2026**. Actual coverage
depends on available, verifiable markets. There is no nighttime execution change.

The candidate is PR20's residual mean correction with PR19's Gaussian width.
The extra kernel shape did not clearly improve on that simpler correction. The
choice is now frozen for this new cohort, including every station and every
accepted result. It was selected using previously inspected historical results;
the new forward cohort is the evidence that matters.

## Training and timing

Fit once from the completed 4,153-event, 21-station GFS archive already received
on October 20, with final CLI labels through October 18. The bias and residual
windows are anchored to the first target and then frozen; this cohort does not
retrain automatically. Actual creation time, input hashes, parent fit hashes,
implementation hash and station parameters are in the committed model artifact.
The historical availability flags remain false. Those records are used only to
seed this newly authorized experimental paper model, never the frozen v3 training
source, historical as-of evidence, or an operational live model.

The current input must be the **previous-calendar-day 00 UTC original GFS run**,
the same input used in the retrospective experiment. Current blended forecasts
cannot be substituted. Receipt identity, station/grid/elevation, Fahrenheit
units, and all 24 hours of the local standard day must validate. A real receipt
before the decision establishes availability for the new forward observation;
it does not establish historical availability of older archive records.

Collection uses the first eligible local 08/11/14/17 hour with an accepted complete
next-day contract partition, once per venue/station/date. Fresh bid/ask receipts,
unchanged contract rules, exact station and day, and a model fitted before the
decision are required. Later snapshots cannot replace a recorded decision.

## Paper positions and accounting

Each venue has a **new, explicitly separate $500 simulated account**. This does
not replenish earlier experiments. Buy at most one whole YES contract per event,
choosing the greatest positive edge of at least 0.05 after observed ask, the
existing conservative fee model, and 0.005 slippage. Missing depth (less than one
contract), fee evidence or quotes means no fill. The full probability vector is
still recorded when cost evidence is incomplete. There is no signing or order API.

Reserve the full simulated cost before recording a fill. Maximum open exposure
is $25, daily realized losses stop at $10, and cumulative net losses stop at $50.
All unsettled exposure must also fit inside the remaining daily and cumulative
loss allowance, preventing pending positions from overrunning a stop. A
cumulative stop stays latched. Results are **simulated paper outcomes**, not
actual execution or proof of realizable profit.

Official venue results must identify every contract, form a single-winner vector,
and arrive after the target window. Kalshi's final rules must equal captured
rules. Saved official responses accompany settlement; no reconstructed prices
or assumed outcome labels are allowed. Missing final results remain pending.
Candidate and same-decision market Brier/log-loss scores average the venues within
each station/date before averaging events. Venue bankrolls and P&L stay separate.

## Persistence and operation

Only `app_settings` keys prefixed `weather_daytime_paper_v1` are used. The compact
book records event IDs, reserved costs, settlements and scores. Full decision and
settlement receipts are separate immutable records and 90-day Actions artifacts.
A compare-and-swap book update reserves exposure and saves an outbox payload
before publishing the immutable event. A crash resumes that payload without a
second fill. Stale writers and immutable conflicts fail closed. Missing books
never initialize automatically; the explicit initialization command cannot
overwrite an existing account. No database schema or RLS changes are needed.

The existing Weather Paper Research workflow runs a separate job, so v3 training
timeouts do not prevent this collector. It uses ordinary dispatch gates and never
backdates a decision or runs a late fallback to fill a missing observation.
The job runs only on main with `WEATHER_DAYTIME_PAPER_ENABLED=true`; disabling
that variable pauses this new experiment without changing the older jobs.
Public forecasts are bounded to 21 original-run requests per invocation (shared
between venues), normally cached by station/run. A provider error persists a
24-hour cooldown; invalid caches are preserved. This does not raise or share the
backfill's quota/ledger or start another archive importer.

The fixed cohort stops taking new positions after November 5; subsequent runs
continue grading pending results. Review coverage, paired probability scores,
paper costs, missing/blocked opportunities and all losses before designing the
next cohort. Fourteen dates are an early diagnostic, not a profitability gate.

Commands from the repository root:

```text
python -m scripts.run_weather_daytime_paper --fit-history <completed-GFS-directory>
python -m scripts.run_weather_daytime_paper --initialize
python -m scripts.run_weather_daytime_paper
```

Only the last command is scheduled. The committed model is immutable. Frozen v2,
v3, the Philly forward plan, earlier loss stops and full-backfill retest remain
unchanged. The rejected nighttime uncertainty candidate remains offline research.
