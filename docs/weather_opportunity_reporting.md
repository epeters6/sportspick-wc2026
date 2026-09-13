# Weather opportunity and performance reporting

The September 13 observer adds evidence to the existing paper experiment. It does not create a new strategy, increase position sizes, lower entry requirements, or enable live orders. The three v2 experiment identities and their hashed implementation remain unchanged.

## What an hourly run now produces

The workflow invokes `python -m scripts.run_weather_observed_cycle --report reports/weather/latest.json`. The wrapper delegates the complete run to the existing research suite. Only after every approach has finished does it inspect the public discovery inputs already captured by that run and the completed account reports.

- `latest.json` and the individual experiment reports remain authoritative and unchanged.
- `opportunities.json` supplements them with the exact captured market identities, missing or overlapping temperature ranges, station/date/venue scope, and separate performance evidence.
- `performance.md` provides a readable scorecard in the GitHub run summary and in the existing 90-day evidence artifact.

No new external requests, database writes, subscription, or trading credentials are needed for these supplementary reports. Exceptions in the observer cannot prevent the already completed paper settlements or forecasts. Diagnostic failures remain visible and cause a failed workflow result; they never replace the original cycle status.

## How this guides development

An incomplete set of temperature ranges is not a valid probability distribution. The observer records the affected public contracts so a discovery defect can be reproduced from the same capture, instead of guessing from a later feed. It distinguishes currently relevant groups from groups outside the configured universe, horizon, or time window. Scope is evaluated at the stated diagnostic time; it is not an invented execution receipt. Outside-window or duplicate-slot cycles may have no discovery capture, which is unavailable evidence rather than zero market opportunities.

The scorecard separates every approach and venue. It distinguishes the account's booked results from the settled trades that passed the forward evaluator, shows unique events and target dates, and keeps the research requirements visible. Rejection counters describe the scope in which the engine recorded them; overlapping counters must not be added into a fabricated count of lost trades. Repeated observations of one target, or the same target on two venues, are not independent wins.

Prioritize fixes that recover valid, liquid opportunities before changing the model or expanding exposure. Changes to the strategy, station mapping, forecast provider, entry selection, or risk limits require a new frozen experiment. Do not tune the current strategy to its first winning or losing day. A small realized profit, a high return on a tiny settled stake, or a passing workflow does not establish a repeatable trading edge.

The original missing short-term price measurements and the retired v1 Chicago station defect remain historical limitations. These reports cannot backfill an observation that was never captured. Live profitability still requires enough future independent outcomes, positive results after costs, and realistic executable liquidity; the observer neither certifies nor forecasts income.
