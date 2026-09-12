# Weather paper research v2

The first forward experiment recorded no trades. Its Chicago forecasts used O'Hare while the inspected contracts settled on Midway. The old reports and manifest remain historical evidence with that known defect.

V2 is a new, predefined experiment suite. It corrects the contract station and reporting-day identity, expands the verified station universe, and measures the effect of entry thresholds and the historical calibration filter. None of the results establish live profitability or permit real orders.

## Fixed approaches

| Approach | Minimum net edge | Historical execution calibration |
| --- | --- | --- |
| Corrected baseline | 5 percentage points | Required |
| Lower edge threshold | 3 percentage points | Required |
| Exploratory forecast | 3 percentage points | Recorded, not enforced |

The exploratory forecast approach uses the same market-blended forecast probabilities as the others, not unadjusted ensemble confidence. All approaches remain paper only. Each has its own $500 simulated account per venue, with 0.5% event risk, 5% open risk, a 2% daily loss limit and a 10% drawdown stop. These accounts must never be added together to suggest deployable capital or combined strategy returns.

Configs are frozen in backend/models/weather/experiments. The original backend/models/weather/experiment.json is retained as the v1 record; use the suite entrypoint for current research:

```text
python -m scripts.run_weather_cycle --suite --report reports/weather/latest.json
```

## Evidence and execution

- Eight vetted locations: New York Central Park, Chicago Midway, Miami, Los Angeles, San Francisco, Denver, Philadelphia and Austin. A location is eligible only when the current venue contract explicitly identifies a supported station and verified daily observation period.
- Forecasts use the contract reporting day. NWS daily climate extrema follow local standard time, including during daylight saving time. Kalshi daily-temperature terms and observed contract closes are checked against that convention; provider/source provenance remains separate.
- Revised station forecasts and MOS calibration use the model name ensemble_station_v2 so old location/day residuals are not pooled into the corrected model.
- Each cycle captures discovery inputs once. Forecasts, calibration inputs and raw market copies are shared consistently across approaches; independent copies prevent one approach's reprice from changing another's initial quote.
- Every proposed fill gets a fresh public orderbook, actual receipt timestamp, whole-contract sizing, fee and slippage reserves, and the same cash/risk limits. No exchange signing or order methods are called.
- Each approach has its own hourly attempt claim, immutable snapshots, candidate IDs, paper ledger and report. Crossing the UTC decision hour prevents new forecasts/fills while settlement and reporting continue.
- Contract buckets must form a complete, non-overlapping outcome partition.
- Rejections distinguish absent minimum edge, missing depth, station identity problems, calibration decisions, rounded size and stale execution timing.
- A filled paper order freezes its exact closing-price obligation payload before the ledger insert. Missing obligations can be recreated from this payload without replaying the trade or resetting observations.
- Reports join closing-price records to the actual fill identity and verify experiment, market, timestamp and cost provenance. Transient database SELECT failures receive bounded retries; exhausted failures remain visible.

## Evaluation

The dashboard provides separate approach selectors. Per-venue paper P&L, risk, unique traded events, target dates, official outcomes and closing-price coverage remain separate. The headline report displays the baseline; experiments contains every approach.

The approaches overlap in markets and outcomes and are therefore correlated. Their selection was informed by the earlier experiment. Their future comparison is exploratory; the 250-event and 45-date requirements are collection floors, not a profitability guarantee. The observe-only approach has not satisfied the historical execution filter. Research readiness never enables live mode.

## Deployment and monitoring

Apply supabase/ops/upgrade_weather_dispatch_experiment.sql for an existing active scheduler. It replaces only the dispatch function and preserves jobs, permissions, audit history and credentials. Do not rerun the installer, which deliberately creates jobs inactive.

The Weather Paper Research GitHub workflow invokes the suite. Its artifacts contain individual approach reports plus the combined index; balances are not summed. The hourly dispatcher and five-minute CLV collector remain separate. The existing monitor must inspect every active experiment and compare milestones by experiment and venue.

Verify production manifest identities and paper mode after publishing, then verify the next eligible forecast hour separately. A successful outside-window report proves report plumbing, not forecasting or fills.

Sources: [NWS observation FAQ](https://www.weather.gov/lot/weather_observations_faq), [Kalshi weather markets](https://help.kalshi.com/en/articles/13823837-weather-markets).
