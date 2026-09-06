# Weather forward experiment: implementation and operating limits

Updated September 6, 2026. This document describes the implemented experiment, not a claim of profitability or permission to trade real money.

## Objective and scope

The research question is whether a fixed weather model and selection rule can earn positive returns after modeled execution costs on daily temperature contracts. The first experiment deliberately tests a narrow universe. It does not implement every weather market, a general arbitrage strategy, or an unrestricted autonomous trader.

The source of truth is [experiment.json](../backend/models/weather/experiment.json):

| Setting | Implemented value |
|---|---|
| Experiment ID | `weather_forward_2026_09_v1` |
| Immutable prediction source | `weather_forward_v1` |
| Mode | Paper only |
| Venues | Kalshi and the existing Polymarket US integration |
| Stations | KNYC (New York/Central Park), KORD (Chicago/O'Hare), KMIA (Miami), KLAX (Los Angeles) |
| Metric | Daily high temperature |
| Target horizon | Today and tomorrow, using station-local dates |
| Decision windows | The 08:00, 11:00, and 14:00 local hours |
| Simulated starting capital | $500 per venue |
| Minimum estimated net edge | An absolute probability advantage of 0.05, or five percentage points, after modeled costs |

Current order construction buys YES contracts on selected temperature buckets. It does not yet implement a separately validated NO-side strategy. A venue needs a supported contract, complete bucket identities, usable quotes, and an eligible station window to generate an entry. An absent trade is an ordinary possible result.

The internal `polymarket` alias means **Polymarket US**. Its gateway, fee schedule, account product, and settlement endpoint are distinct from the international Polymarket CLOB. This workflow uses public market-data clients and contains no exchange signing credentials. Account eligibility and any future live execution would need a separate review. The US [market settlement API](https://docs.polymarket.us/api-reference/markets/get-market-settlement) returns the settlement for the exact market slug.

## Experiment identity and historical accounting

[weather_experiment.py](../backend/trading/weather_experiment.py) registers a manifest in `app_settings` containing the configuration, implementation hash, and creation timestamp. Important model, execution, fee, station, and settlement implementation files are hashed with normalized line endings. If those inputs change under an existing experiment ID, registration fails. A material strategy change therefore needs a new ID with the prior record preserved.

The two $500 balances are newly declared simulated capital. They are neither deposits nor a recovery of earlier losses. Legacy weather and sports records remain intact. Experiment accounting reads only paper bets bearing the experiment ID and separates venues.

For each venue:

```text
paper equity = declared seed + verified realized P&L
available cash = max(0, paper equity - reserved entry costs)
```

Equity here is an accounting value, not a liquidation value marked to current bid prices. Open positions reserve their recorded entry costs. Settled records must pass exact settlement verification, timestamp checks, and venue provenance. Unverified records quarantine the account and retain the applicable cash reservation; they do not silently become winners or spendable funds.

The account risk rules are:

| Control | Rule | At the initial $500 equity |
|---|---|---:|
| Event entry budget | At most 0.5% of current equity, further limited by cash and remaining open-risk capacity | $2.50 |
| Open risk | At most 5% of current equity | $25 |
| Daily loss stop | Sum of negative realized settlements on the current UTC date reaches 2% of the seed | $10 |
| Experiment loss stop | Equity falls to 90% of the declared seed or below | $450 equity |
| Accounting stop | Any quarantined evidence or non-positive available cash | No entry |

The 10% experiment stop is measured from the seed, **not a trailing peak-equity drawdown**. The daily rule sums losses without offsetting them by wins. It is based on settlement date in UTC. These distinctions matter when interpreting a stopped account.

The pipeline also rejects another open exposure for the same station/date/metric across venues. It is not permitted to treat two contracts on the same weather event as independent risk. This conservative rule can make venue processing order affect which venue receives an entry; the system is not claiming optimal cross-venue capital allocation.

## What happens in a cycle

[run_weather_cycle.py](../scripts/run_weather_cycle.py) runs hourly through [weather_cycle.yml](../.github/workflows/weather_cycle.yml), scheduled at minute 17. Local timezone checks restrict forecasting to the fixed station windows; settlement and reporting can proceed at other hours. GitHub schedules can be delayed, so the actual decision timestamp is evidence, while the configured cron time is only an intention. [GitHub scheduling troubleshooting](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)

The stages register the experiment, settle open weather positions, backfill meteorological diagnostics, resolve official prediction labels, forecast when eligible, settle again, evaluate the forward record, and publish account status. Stage failures are recorded individually so a failed forecast does not suppress settlement or reporting. A failed manifest blocks new forecasts while outcome processing remains available.

One database claim permits only one forecast attempt per experiment and UTC hour across retries. A failed attempt remains an attempted slot; retrying later in that hour cannot select a more favorable quote and silently replace it.

The cycle writes `reports/weather/latest.json` and a durable `weather_cycle_latest` value in `app_settings`. The API exposes the durable report at `/weather/experiment`. GitHub uploads report evidence even on failure and retains artifacts for 90 days. A `healthy` result describes completed operations; `live_ready` remains false, and a healthy report is not a profitable strategy.

The separate [GitHub CLV workflow](../.github/workflows/clv_checkpoints.yml) is the primary checkpoint observer for this forward experiment. It runs approximately every five minutes and fetches weather prices through unsigned public Kalshi and Polymarket US clients. Its timestamps retain exchange-versus-local-receipt provenance. Trading credentials are intentionally absent; legacy signed Kalshi sports observations may be unavailable from this GitHub job.

**Deployment prerequisite:** before the first forward cycle, deploy the updated [Supabase edge observer](../supabase/functions/clv-checkpoints/index.ts) with the `metadata.experiment_id` exclusion, or disable its existing cron. The edge observer is legacy-only; an older deployed version can otherwise write incompatible checkpoint evidence for forward rows. Source edits do not update a deployed function or cron. No observer deployment or cron change is performed by this document or the GitHub configuration edit.

## Forecasts, costs, and paper fills

The [weather pipeline](../backend/models/weather/sync_weather.py) combines ensemble forecasts, station-level bias and residual-error estimates, and market shrinkage. It evaluates a complete mutually exclusive bucket vector and applies a more conservative, venue-specific calibration to the selected execution candidate. Small or unprofitable calibration cohorts block entries rather than forcing activity.

The configuration and learning algorithm are frozen; calibration estimates may adapt to newly available **prior** observations under that fixed algorithm. [Execution calibration](../backend/ml/weather_execution_calibration.py) accepts an explicit timezone-aware `as_of` cutoff, defaulting to current UTC, and excludes official outcome labels resolved after it. Closing-price training requires a completed observation no later than the cutoff and a valid earlier entry. History remains venue-specific, and the cutoff is included in calibration metadata.

[Station calibration](../backend/ml/weather_mos.py) uses only completed prior station-local target dates, also earlier than the UTC cutoff date, with `weather_verification.updated_at` no later than the cutoff. Unknown stations and missing/invalid update timestamps are excluded. Its cache includes the cutoff so a newer fit is not reused for an earlier decision. These guards reduce look-ahead; they do not reconstruct immutable historical versions of the upstream forecast and observation feeds. A retrospective backtest would still require those point-in-time inputs.

Running METAR temperature extremes are recorded as provisional diagnostics. They do not settle contracts, zero out bucket probabilities, or alter the market baseline. The final venue outcome remains authoritative. Kalshi explains that weather contracts can depend on different reporting products; daily climate reports and hourly observations must not be substituted for one another. [Kalshi weather-market rules](https://help.kalshi.com/en/articles/13823837-weather-markets)

Before a paper fill, the selected order book is refreshed. Missing ask depth means no assumed liquidity. The pipeline rejects invalid quotes, significant adverse repricing, or an edge that disappears after repricing. Size is limited to whole contracts, visible depth, available cash, and the account budget.

The frozen `paper_allow_receipt_timestamp=true` setting explicitly permits response-receipt freshness when a public book lacks an exchange timestamp, including the Polymarket US public response. Paper simulation uses the two-second freshness bound and records `book_timestamp_source=local_receipt` plus `book_received_at`; it does not invent an exchange publication time. This is a simulation assumption, not verified exchange latency or assurance that the quoted orders remained available. Where an actual exchange timestamp exists, its provenance is retained.

The effective entry cost is:

```text
q_exec = current ask + conservative per-contract fee budget + $0.005 buffer
```

The [fee model](../pavlov/pipeline/fee_model.py) records its version, venue product, source, fee type, multiplier, rounding assumptions, and rebate treatment in the evidence. Kalshi requires verified applicable schedule metadata and models buyer balance alignment; it does not assume every market has identical fees. The default paper rounding precision represents a non-direct account. [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)

The Polymarket US implementation uses the published taker coefficient of 0.06 and accounts for its cumulative rounding rules. Conservative whole-contract budgeting intentionally over-reserves rather than crediting maker or volume rebates. This is the US schedule, not international CLOB pricing. [Polymarket US fee schedule](https://docs.polymarket.us/fees)

Recorded paper cost includes the fee allowance and buffer. A winning binary position has a $1-per-contract payout; realized paper P&L is payout minus its recorded cost. Actual future venue commissions would supersede the estimates. Neither observed order-book depth nor simulated fills prove live fillability, latency, queue position, or feasible executions through outages.

## Immutable records and forward evaluation

The pipeline saves every bucket in a separate immutable pre-execution snapshot, including unselected candidates. IDs incorporate the experiment, run, event, and outcome. A fingerprint protects the saved prices and probabilities against retry replacement. Official grading may add outcome evidence; it must not replace the original decision. The older mutable latest-view source is retained for diagnostics and is not the forward performance record.

[weather_forward_report.py](../backend/trading/weather_forward_report.py) verifies complete normalized bucket vectors, exact official venue/market labels, one winning bucket, decision identity, cost evidence, and trade-to-snapshot links. It quarantines inconsistent evidence and treats incomplete database reads as a failed research requirement.

Forecast Brier scores average bucket errors within snapshots, snapshots within station/date/metric events, and events within venues. Multiple buckets or repeated forecasts of the same event therefore do not become many independent successes. The report separately evaluates verified paper fills, because better pooled forecast error does not itself establish profitable selection.

Each venue's research requirements are:

1. Complete evidence reads with no quarantined forecast or trade evidence.
2. At least 250 distinct **traded** station/date/metric events and 45 distinct traded target dates.
3. A positive lower bound on the exploratory 95% net-ROI interval, using 2,000 bootstrap samples of whole target dates.
4. Positive net closing-price advantage on the actual traded segment.
5. Complete closing-price observations for at least 80% of traded events, including at least 30 events.
6. Verified immutable forecast snapshots linked to every assessed trade.

Closing-price evidence must match the market, side, trade, experiment, and effective entry cost, occur after entry and before settlement, and be within ten minutes of its required checkpoint. Venue reports are separate; the overall research-ready flag requires both venues to satisfy their criteria.

These sample requirements are collection floors. The bootstrap preserves within-date dependence but does not remove all cross-date weather dependence, seasonality, repeated-monitoring bias, or strategy-selection bias. A small apparent edge can require considerably more evidence. `research_evidence_ready` only means that the implemented research criteria pass. All forward reports keep `live_ready=false`; there is no automatic funding or live promotion.

## Data licensing and the route to real-money research

The current ensemble client uses Open-Meteo's hosted ensemble API. Its free hosted tier is limited to non-commercial use, and its commercial ensemble access requires the Professional plan or higher. Treat that free service as a paper-research dependency; before using it for an actual profit-seeking deployment, obtain appropriate commercial service or migrate the inputs. The underlying data license and permission to use the free hosted service are separate questions. [Open-Meteo pricing and licensing explanation](https://open-meteo.com/en/pricing)

NWS offers its API data free for any purpose with reasonable rate limits. Direct NOAA/NWS inputs are a possible alternative, but replacing the current multi-model ensemble feed needs engineering and a newly identified experiment; a single forecast endpoint is not an equivalent ensemble. [NWS API documentation](https://www.weather.gov/documentation/services-web-api)

Before any real-money phase, separately establish the data-service permissions, usable venue account, accurate live commissions, order/fill reconciliation, withdrawal and balance behavior, and an approved capital limit. No subscription, account funding, deployment, or real order is performed by the paper workflow. Passing research gates should lead to a review of these requirements, not an unattended switch to live mode.
