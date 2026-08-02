# Kalshi / Polymarket Arbitrage Scanner

Local research scanner for identifying the same binary proposition on Kalshi and
Polymarket. It does not place live orders or connect to a wallet or brokerage.

The scanner is deliberately fail-closed. A displayed opportunity must pass a
hard settlement-identity check and have fresh executable ask depth on both
venues. Discovery prices are never treated as fills.

## Quick start

From the repository root:

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r arbitrage\requirements.txt
.\arbitrage\start.ps1
```

Open <http://127.0.0.1:8000>. The launcher restarts Uvicorn after a crash for as
long as that PowerShell session remains open. It is not an operating-system
service.

For one foreground process instead:

```powershell
.\backend\.venv\Scripts\python.exe -m uvicorn arbitrage.backend.main:app --host 127.0.0.1 --port 8000
```

## Integrity model

- Matching first rejects different event types, subjects, geographies, dates,
  thresholds, award categories, offices, and endorsement objects.
- Kalshi and Polymarket order books are fetched only after strict matching.
- Each leg uses visible ask depth and a size-weighted average execution price.
- Venue fees and a configurable cross-venue execution/legging buffer are
  subtracted before an edge is considered profitable.
- Quotes older than the configured limit, incomplete books, and suspicious gaps
  are excluded.
- Paper positions reserve capital and record expected PnL separately. Portfolio
  balance changes only after both venues return compatible final results.
- Legacy reference-price history is retained but excluded from verified stats.

## Important configuration

| Variable | Default | Meaning |
| --- | ---: | --- |
| `ARB_REFRESH_INTERVAL` | `30` | Seconds between completed scan attempts |
| `ARB_MATCH_AUTO` | `85` | Minimum confidence for automatic strict matches |
| `ARB_MATCH_REVIEW` | `70` | Candidate review threshold |
| `ARB_QUOTE_SIZE` | `25` | Contracts/shares used for depth VWAP |
| `ARB_MIN_EXECUTABLE_SIZE` | `1` | Minimum visible paired size |
| `ARB_MAX_QUOTE_AGE` | `20` | Maximum quote age in seconds |
| `ARB_EXECUTION_BUFFER` | `0.01` | Per-pair execution/legging reserve in dollars |
| `ARB_SHADOW_MAX_CONTRACTS` | `25` | Maximum paper position size |
| `ARB_SHADOW_BANKROLL` | `10000` | Paper starting bankroll |
| `ARB_HISTORY_RETENTION_DAYS` | `14` | Retention for current verified opportunities |
| `ARB_ADMIN_TOKEN` | empty | Token required for remote mutation requests |
| `ARB_CORS_ORIGINS` | local URLs | Allowed browser origins |

Kalshi fee multipliers are hydrated from the series endpoint. The default fee
mode is conservative taker execution. Polymarket token IDs are mapped by their
explicit YES/NO outcome labels; non-binary markets are rejected.

## API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/health` | GET | Freshness, last error, scan count, data version |
| `/api/opportunities` | GET | Current executable opportunities |
| `/api/config` | GET/POST | Read or update runtime configuration |
| `/api/stats` | GET | Current-version history and paper summary |
| `/api/shadow-bets` | GET | Paper ledger with expected and realized PnL |
| `/api/refresh` | POST | Run a complete refresh immediately |
| `/api/fee-preview` | GET | Inspect fee calculations |

Mutation routes are available from localhost. Remote callers require
`X-Arbitrage-Admin-Token` matching `ARB_ADMIN_TOKEN`.

## Tests

```powershell
.\backend\.venv\Scripts\python.exe -m unittest discover -s arbitrage\tests -p 'test_*.py' -v
```

The suite covers settlement-identity false positives, order-book parsing, depth
VWAP, fail-closed calculation, versioned scan history, and paper settlement.

## Key modules

- `backend/matcher.py`: proposition identity validation and matching
- `backend/execution.py`: Kalshi/Polymarket order-book hydration
- `backend/calculator.py`: depth, fees, buffer, and paired-edge calculation
- `backend/settlement.py`: two-venue result verification
- `backend/database.py`: versioned history and paper accounting
- `backend/main.py`: fail-closed refresh lifecycle and API
