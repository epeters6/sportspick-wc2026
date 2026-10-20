# QuantBet Weather

Private weather-market research and automated paper trading on **Kalshi and Polymarket US**. The active experiment concentrates on daily high temperatures at KNYC, KORD, KMIA, and KLAX. Existing MLB, football, influencer, and arbitrage research remains available as history; it is no longer the scheduled development focus.

**Current status: paper research, profitability unproven.** The project must demonstrate reliable official settlement, realistic execution costs, and positive forward performance before real capital is considered. This change does not authorize live orders or erase historical paper losses.

Read the [implemented weather strategy and its limits](docs/weather_strategy.md) for the fixed universe, risk rules, calibration cutoffs, paper cost assumptions, and forward evaluation criteria.

## Active weather cycle

```text
Supabase Cron: hourly weather dispatch at :07, bounded watchdog at :27
  └─ GitHub Actions: weather_cycle.yml (native :17 cron remains a fallback)
  └─ python -m scripts.run_weather_cycle --report reports/weather/latest.json
       ├─ verify and settle existing paper positions using official venue outcomes
       ├─ forecast only within fixed station decision windows
       ├─ evaluate the frozen weather experiment and record decisions
       └─ persist experiment status and upload evidence even after stage failures

Supabase: forecasts, immutable experiment records, positions, official labels
  └─ FastAPI: /weather/experiment and /weather-predictions
       └─ Next.js: /weather (default home), /trading, readiness and validation
```

The experiment declares **$500 of simulated seed capital per venue**. Its balance is separate from the legacy paper ledger, whose losses remain available in Positions & history. Forecast and trade decisions must retain their experiment identity; a changed configuration belongs to a separately identified experiment.

The current Polymarket integration is **Polymarket US**, not the international CLOB. Venue credentials, fees, settlement rules, and account eligibility must be handled according to the integrated venue. A similarly named contract on another venue does not establish identical settlement rules.

### Operating commands

From the repository root, with the Python environment activated and configured:

```bash
python -m scripts.run_weather_cycle --report reports/weather/latest.json
python -m unittest discover -s backend/tests -p "test_*.py" -v
```

The cycle writes paper research data. Run it against the intended database only. The orchestration is paper-only and isolates settlement/reporting from forecast failures. A healthy cycle means the operations completed, not that the strategy is profitable. Review timestamps, failed stages, quarantined settlements, and per-venue balances before interpreting results.

Configure the [Supabase dispatch scheduler](supabase/ops/WEATHER_DISPATCH.md) separately; its installation starts inactive until the dedicated repository credential is provisioned and activation is verified. GitHub native cron missed eligible hours during initial activation, so it remains a fallback rather than the primary clock. External dispatch can still encounter runner queues. Expired requests are skipped, and weather execution requires enough time before the next UTC hour. The fixed station decision hours remain 08:00, 11:00, and 14:00 local.

## Setup

1. Install Python dependencies into a virtual environment:

   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r backend/requirements.txt
   pip install -r pavlov/requirements.txt
   ```

2. Copy `.env.example` to `.env` and configure the intended database. The dedicated weather paper cycle uses public market-data and settlement clients, without exchange signing credentials. Never commit `.env`, private keys, or service-role credentials. Apply the repository's required Supabase migrations in order for a new database; the initial schema alone does not include the later weather/trading features.

3. Keep all live-execution switches disabled:

   ```dotenv
   LIVE_TRADING_ENABLED=false
   POLYMARKET_LIVE_ENABLED=false
   AUTO_BET_ENABLED=0
   POLY_AUTO_BET_ENABLED=0
   POLY_MLB_AUTO_BET_ENABLED=0
   POLY_MLB_INGAME_ENABLED=0
   ```

4. Start the API and dashboard in separate terminals:

   ```bash
   uvicorn backend.api.main:app --reload
   ```

   ```bash
   cd frontend
   npm ci
   npm run dev
   ```

The dashboard uses `NEXT_PUBLIC_API_URL`, defaulting to `http://localhost:8000`. Open `http://localhost:3000`; it routes to the weather workspace. Configure the real API origin when deploying the dashboard. Account secrets belong on the server, never in `NEXT_PUBLIC_*` values.

## GitHub Actions

| Workflow | Trigger | Purpose |
|---|---|---|
| `weather_cycle.yml` | Supabase hourly at :07 with bounded :27 watchdog; native :17 fallback; manual | Active weather paper experiment, independent settlement, report artifacts |
| `clv_checkpoints.yml` | Supabase every five minutes; native cron fallback; manual | Primary forward-weather closing-price observer, using public venue books; does not place orders |
| `unit_tests.yml` | Push, pull request, or manual | Backend tests and dashboard type checking without production secrets |
| `sync_scrape.yml` | Manual only | Legacy influencer and sports collection |
| `sync_ml.yml` | Manual only | Legacy broad ML cycle and sports shadow settlement |
| `pavlov_mlb.yml` | Manual only; defaults to `resolve` | Legacy MLB resolution or explicitly selected cycle |
| `daily_report.yml` | Manual only | Legacy report, including its existing Discord delivery |
| `worldcup_sync.yml` | Manual only | Historical football result reconciliation |

The new weather workflow sends no Discord messages. A manual legacy workflow may still contain its original external-reporting behavior; inspect it before dispatching. Historical settlement access remains available even though new sports cycles have no cron schedule.

Weather workflow secrets:

| Secret | Use |
|---|---|
| `SUPABASE_URL` | Database API origin |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side research persistence |
| `SUPABASE_ANON_KEY` or legacy `SUPABASE_KEY` | Existing environment validation/client compatibility |
| `OWM_API_KEY` | Optional existing weather provider |

Exchange signing keys are intentionally absent from the new paper workflow. Actions artifacts retain the cycle reports for 90 days. The latest status is persisted in the database for the dashboard; local report files alone are not treated as deployed API state. Source edits take effect remotely only after an authorized push/deployment. This repository can use free service tiers, but quotas and provider terms still apply; free hosting does not eliminate execution fees or prove a trading advantage.

**Before the first forward-weather cycle in a new deployment:** deploy the updated `supabase/functions/clv-checkpoints` observer that excludes records with `metadata.experiment_id`, or disable its existing cron. GitHub is the primary observer for the new experiment; the Supabase edge observer is legacy-only. Leaving the old edge code active can write incompatible checkpoint evidence into the new experiment. For the existing project, the corrected edge function was deployed and verified on September 6, 2026, and its legacy cron was paused. Exchange signing credentials are not required by the public weather observer.

## Reading the dashboard

- **Weather research:** current experiment status, per-venue paper balances, and the latest available model bucket records. Loading failures are shown as unavailable rather than a healthy status or zero balance.
- **Positions & history:** defaults to weather and preserves historical sports filters. Aggregate legacy metrics cover all domains; the table filters only the latest fetched records.
- **Readiness gates:** research and execution requirements. Passing a numeric gate does not independently authorize funding or live orders.
- **Historical sports research:** the original MLB, model, influencer, match, and scraper routes remain accessible.

The recent prediction feed is not a complete performance sample. Bucket rows from the same event are correlated, and pooled accuracy is not trading profit. Performance decisions must use immutable forward records, officially resolved outcomes, modeled fees and executable prices, and event-aware uncertainty. Do not loosen gates just to create activity or reset a losing experiment under the same identity.

## Code map

| Location | Responsibility |
|---|---|
| `scripts/run_weather_cycle.py` | Dedicated weather orchestration and durable report |
| `backend/models/weather/` | Venue discovery, station forecasts, calibration, and weather decisions |
| `backend/trading/` | Accounting, settlement verification, risk controls, and experiment support |
| `backend/api/` | Dashboard API |
| `frontend/app/weather/` | Weather-first workspace |
| `supabase/migrations/` | Schema and access-policy history |
| `backend/tests/` | Financial, model, and orchestration regression checks |
| `reports/weather/` | Generated cycle evidence; not source code |

The repository began as SportsPick Tracker for World Cup 2026. Its older scrapers, sports models, and separate arbitrage scanner are preserved for reference and historical settlement, rather than expanded as part of the active weather experiment.

## Offline weather probability screen

Use the saved, validated original-run history to reject weak probability candidates before spending future collection time on them:

```bash
python -m scripts.run_weather_probability_screen \
  --history reports/weather_historical_calibration/gfs_2026_all_stations/dataset.json \
  --market-history reports/weather_segment_input.json \
  --expected-history-hash a894ca6ae9a527ca3b8fcddc2fc688d3698e47f3b281f4a2d70d5a151de6711e
```

This command uses saved files only. It fits station-specific overnight-low residual distributions through August 29, 2026, then scores a fixed residual model and a fixed 50/50 market blend on later, officially settled next-day market vectors. It enforces the original GFS run, station, standard-day window, complete bucket partition, and quote timing. Repeated snapshots and venues share one station/date weight. Outputs include immutable specifications, model and implementation hashes, paired calendar-block uncertainty, rejected evidence, and separately labelled quote-only diagnostics.

The archive's historical public availability and the historical publication time of final NWS observations are unverified, so this is an exploratory retrospective screen. It creates no forward forecasts, sends no database writes, and changes no existing model, frozen study, risk stop, workflow, or trading setting. A candidate that trails the same market baseline fails the screen. Even a better retrospective candidate requires a separately frozen prospective trial; nothing is promoted automatically. Reuse the saved receipt cache when refreshing history and preserve any corrupt or conflicting evidence for investigation.

### Decision-time inputs and conservative market corrections

```bash
python -m scripts.run_weather_market_correction \
  --history reports/weather_segment_input.json \
  --output reports/weather_market_correction
```

This separate research candidate uses the GFS, HRRR, ECMWF, observations, and forecast changes actually saved before each decision. It does not interpret download age as model-run age. A regularized correction to the market's log probabilities is fit on earlier dates; the correction weight is selected on a later calibration partition, with label-availability embargoes before subsequent decisions. The market itself is an explicit fallback. A nonzero correction must improve both calibration Brier score and log loss, and each bucket's probability change is capped at five percentage points. High/low outcomes and venues share one station/date weight.

The final chronological partition includes comparison with the same market baseline and quote-only cost sensitivity. Costs use the saved ask-based fee-inclusive execution estimate, require fresh fee/quote evidence and two displayed contracts for a one-contract diagnostic, and add zero, one-cent, and three-cent slippage scenarios. Zero-fill and adverse-selection scenarios remain explicit. No price-only calculation is called realized profit, and no candidate is automatically promoted. Old data already inspected in research is not a permanent unseen holdout. Prospective prediction additionally rejects models created after their decisions.

For longer original-run history, the bounded archive importer supports all 21 stations and explicit local decision hours:

```bash
python -m scripts.run_weather_decision_archive \
  --start 2026-09-30 --days 2 --hours 8 11 14 17 --lead-days 1 \
  --request-budget 60 --location-budget 700 \
  --cache-dir reports/weather_historical_calibration/receipts \
  --output reports/weather_decision_archive/extended_runs_2026_09_30
```

Use `--plan-only` to inspect the request/location count before a larger range. Resume with identical plan arguments and the same cache; `--request-budget 0 --location-budget 0` verifies cached reconstruction without downloads. The importer enforces per-run and persistent daily location limits, preserves unavailable-run receipts, and fails closed on corrupt evidence or identity mismatches. Dataset/report snapshots are immutable; `latest.json` identifies the current dataset file. Run one importer process at a time.

Original-run selection uses nominal release lags (six hours for GFS/ECMWF and three for HRRR), not verified historical publication times. Next-day HRRR uses the documented 48-hour 00/06/12/18 UTC cycles; ordinary hourly runs cover only 18 hours. Missing full-day coverage stays missing, including unavailable earlier runs needed for revisions. These archived rows remain offline-only and are not silently mixed into the correction model's captured-before-decision dataset. Sources: [Open-Meteo original runs](https://open-meteo.com/en/docs/single-runs-api), [GFS/HRRR horizons](https://open-meteo.com/en/docs/gfs-api), [model availability](https://open-meteo.com/en/docs/model-updates), [IEM NWS daily climate](https://mesonet.agron.iastate.edu/json/cli.py).

### Full April–October archive and retest

```bash
python -m scripts.run_weather_full_backfill \
  --archive reports/weather_decision_archive/full_2026_04_03_2026_10_19 \
  --cache-dir reports/weather_historical_calibration/receipts \
  --market-history reports/weather_segment_input.json \
  --output reports/weather_full_history_retest
```

Each invocation resumes the same 200-day plan (April 3–October 19), across all 21 stations and four local decision hours. The plan contains 2,400 batched requests, 43,000 location units and 16,800 possible decision snapshots. One invocation downloads at most 1,000 location units. Persistent ledgers enforce 3,000 units per rolling hour and 6,000 per rolling 24 hours, leaving free-tier headroom for normal collection. October 20 retains a 2,000-unit daily allowance because the earlier GFS expansion was collected that day. Run once every four hours; do not run importers concurrently. `--cache-only` reconstructs evidence without any downloads. Preserve stale locks for inspection, never delete corrupt caches or budget ledgers to bypass a stop.

The worker records progress and only starts the final retest after every planned request has been processed. Unavailable runs and missing official labels remain explicit gaps. The retest requires all 21 stations in each partition, fits a fixed ridge correction to the three-model temperature ensemble through August 29, calibrates residual bias/uncertainty on August 31–September 18, and evaluates temperature errors on September 20–October 19. Snapshot duplication cannot reduce ridge regularization. Report high and low errors against each raw model and the equal-weight ensemble.

Where saved market quotes exist, the archived temperature distributions are compared with both market probabilities and the probabilities originally saved by the current model. A bounded market blend is selected only on September 20–October 1, with label-availability checks before the October 3–19 market test. Missing older market prices are never reconstructed. This is exploratory retrospective analysis with unverified archival publication times, not an as-of backtest or proven profit. Fee/depth/slippage diagnostics remain separate from forecast accuracy. Final reports and model artifacts are immutable; `completion.json` prevents repeated automatic retraining. No production model, frozen experiment or risk stop is changed.
