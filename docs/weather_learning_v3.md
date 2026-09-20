# Weather learning v3

This is a separate, continuously evaluated research model. It cannot submit orders,
does not write `autobets`, does not change v2 manifests, and cannot reset the v2
drawdown stop. It uses a new `model_predictions.source`, `weather_station_learning_v3`,
and publishes its status under `app_settings.weather_station_learning_v3_latest`.
No new database permissions, tables, paid subscriptions, or credentials are required.

## What learns

The target is each contract's **official YES/NO result**, not a daily maximum
inferred from intermittent METAR samples. The initial bootstrap uses only the
corrected `weather_forward_v2` snapshots. Other arms repeat that evidence and are
excluded; legacy city-mapped forecasts are also excluded.

Two candidates are trained: regularized logistic regression and histogram gradient
boosting (100 iterations, 7 leaves, fixed hyperparameters). An independent
chronological calibration partition fits the probability temperature and chooses
the candidate. The final date partition is reserved for reporting. Target dates
never cross partitions, and entire vectors are purged unless their labels were
available before the following partition's earliest decision. This naturally
leaves an embargo for next-day forecasts and delayed settlement.

All hourly snapshots of one station/date/metric carry one combined training weight,
including overlapping venues. Repeated buckets and repeated hourly forecasts are
not advertised as independent training events. Every vector must be exhaustive,
contain unique contracts and have exactly one official winner. Incomplete or
contradictory labels remain excluded until resolved.

Eight dates suffice to run a small diagnostic experiment; they do not justify a
complex model or a profitability claim. The report explicitly counts rows,
station-days, dates, enriched training rows, and the train/calibration/test split.

## New weather inputs

* GFS Global, HRRR and ECMWF hourly forecasts at the exact station's coordinates
  and elevation, checked through the Aviation Weather Center station registry.
* Running temperature extremes, current temperature, recent temperature trend,
  dew point and wind from that station's METAR/SPECI observations. Actual `obsTime`
  is preferred to the rounded report hour. Observations older than 90 minutes
  cannot shift the current forecast.
* Cloud forecasts, model disagreement, time remaining, observation coverage and
  missing-data flags. Nearby stations provide temperature differences and trends;
  their temperatures never substitute for the settlement station.
* A provisional nowcast estimates the remaining hourly path by comparing the
  latest observation with the forecast **at that hour**, decaying the adjustment
  over six hours. It preserves the observed running extreme. Missing observations
  never replace an earlier predicted daily maximum with an evening temperature.

The provisional normal distribution is an explicitly untrained comparison model,
not an execution probability. The classifiers learn official contract outcomes
from its probability and the individual weather features. Existing bootstrap rows
lack those richer features; the report makes that limitation explicit. Equal-model
averaging in the provisional prior is not presented as learned model weighting.

## Contract identity and expansion

The independent registry supports 21 station identities and nearby observation
stations. An actual contract is admitted only with explicit station evidence in
its rules or structured fields, a supported authority, Fahrenheit daily-extreme
semantics, and a verified reporting window. A familiar city title is insufficient.
Conflicting stations, unknown precision, unsupported products and incomplete
temperature ladders are rejected and counted.

The captured contract retains the full rules, rules hash, source, station,
reporting timezone, observation window, integer-F bucket bounds, and registry
version. Daily NWS climate reporting follows standard time. A TWC daily contract
requires a CLI location reference and a close matching the standard-day end.
This logic must not be generalized to TWC hourly or international Polymarket
markets. This project uses **Polymarket US**. Changed rules prevent automatic
labelling and require investigation.

## Operation

The existing hourly weather workflow runs v3 after the frozen paper suite.
Collection takes one immutable hourly claim and records decisions at station-local
08:00, 11:00, 14:00 and 17:00, for target dates up to two days ahead. Predictions,
model ID, training timestamp, source receipts, input hashes, quotes, fees and
available depth remain attached to each bucket snapshot. Weather is fetched before
the fresh quote and decision timestamp. No later observation can be added to an
earlier prediction.

Every run resolves past contracts through their exact venue market IDs. It records
when this collector obtained the final label separately from official settlement
time. Public API reads are paced and cached; rate limits do not trigger aggressive
retries. Forecasts and model artifacts persist through the workflow cache. The
models retrain after 24 hours or when their implementation cache changes.

The rolling holdout report is separate from the **forward report**, which scores
saved probabilities from models already trained when the decision was made. Both
include Brier score, log loss, reliability bins and upper/lower-tail diagnostics.
The price-only diagnostic uses the first qualifying quote per station-day and
includes captured fees/slippage, but does not claim that a fill occurred.

Research review requires at least 45 future dates and 250 station-days, followed by
verified executable economics. Meeting those thresholds does not automatically
activate trading. This runner has no order-execution capability.

```powershell
# Use the project's normal Python environment.
python -m scripts.run_weather_learning --collect

# Reproduce a training run from an exported model_predictions JSON array.
python -m scripts.run_weather_learning --history reports/weather_v3/bootstrap.json --no-persist --force-train
```

Artifacts: `reports/weather_v3/latest.json`, `collection.json`, `snapshots.jsonl`,
and `model/training.json`, `training.md`, `model.joblib`. Only load model files
produced by this trusted repository; the loader checks the recorded SHA256 and
scikit-learn version. This is not a general-purpose uploaded-pickle loader.

## More historical data

`PublicFeeds.archived_forecast` supports explicitly selected operational model runs.
An archive initialization timestamp is **not** proof of publication time. Historical
augmentation must establish availability, station, reporting window and official
labels. Retrospectively fetched weather, reanalysis, final market prices and
hindcasts must not be attached to old decisions and called a profitable backtest.
The immediate bootstrap deliberately uses the timestamped evidence already held.

References: [Kalshi rules](https://help.kalshi.com/en/articles/13823837-weather-markets),
[AWC data API](https://aviationweather.gov/data/api/),
[Open-Meteo GFS/HRRR](https://open-meteo.com/en/docs/gfs-api),
[individual model-run archive](https://open-meteo.com/en/docs/single-runs-api),
[probability calibration](https://scikit-learn.org/stable/modules/calibration.html).
