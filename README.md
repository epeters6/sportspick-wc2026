# QuantBet Weather

Private weather-market research and automated paper trading on **Kalshi and Polymarket US**. The active experiment concentrates on daily high temperatures at KNYC, KORD, KMIA, and KLAX. Existing MLB, football, influencer, and arbitrage research remains available as history; it is no longer the scheduled development focus.

**Current status: paper research, profitability unproven.** The project must demonstrate reliable official settlement, realistic execution costs, and positive forward performance before real capital is considered. This change does not authorize live orders or erase historical paper losses.

Read the [implemented weather strategy and its limits](docs/weather_strategy.md) for the fixed universe, risk rules, calibration cutoffs, paper cost assumptions, and forward evaluation criteria.

## Active weather cycle

```text
GitHub Actions: weather_cycle.yml (hourly at :17)
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

GitHub timing is approximate. The configured hourly cycle supports decision windows at 08:00, 11:00, and 14:00 in each station's local time; forecasting outside eligible windows is skipped. It is not a continuous low-latency execution service.

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
| `weather_cycle.yml` | Hourly at :17 UTC, or manual | Active weather paper experiment, independent settlement, report artifacts |
| `clv_checkpoints.yml` | Every five minutes, or manual | Primary forward-weather closing-price observer, using public venue books; does not place orders |
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

**Before the first forward-weather cycle:** deploy the updated `supabase/functions/clv-checkpoints` observer that excludes records with `metadata.experiment_id`, or disable its existing cron. GitHub is the primary observer for the new experiment; the Supabase edge observer remains legacy-only. Leaving the old edge code active can write incompatible checkpoint evidence into the new experiment. The GitHub CLV workflow also omits trading credentials; legacy signed Kalshi sports observations may be unavailable there and remain the responsibility of the legacy observer. These deployment prerequisites are documented, not performed by changing these files.

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
