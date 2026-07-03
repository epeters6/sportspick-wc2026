# pavlov-mlb-bot

Python project extending **pavlov-weather-bot** patterns for MLB + Polymarket workflows.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Edit .env — all variables in .env.example are required except commented optional ones.
```

**Polymarket US**: the copied `pipeline/polymarket_client.py` uses the official SDK and expects **`POLY_KEY_ID`** and **`POLY_SECRET_KEY`** (aliases `POLYMARKET_*` are also read). `POLYMARKET_API_KEY` in `.env.example` is reserved for legacy notes; the client does not use a single API-key env on its own.

**Persistent state (Railway)**: attach **one** volume and point state at it so **everything** written under `logs/` and `data/` survives redeploys:

- Set **`STATE_DIRECTORY`** to the volume mount path, **or**
- Omit `STATE_DIRECTORY` and rely on **`RAILWAY_VOLUME_MOUNT_PATH`** (Railway sets this when a volume is attached) — `config.py` copies it into `STATE_DIRECTORY`.

Do **not** set `STATE_DIRECTORY` to the app image directory (e.g. `/app`) while the volume is mounted elsewhere; the bot will log a warning. Runtime files include `logs/positions.json`, `logs/signals.json`, `data/games_cache.json`, `data/umpire_cache.json`, pending-signal stores under `data/`, etc. Startup calls **`ensure_state_dirs()`** so `logs/` and `data/` exist on the volume.

## Run

```bash
python main.py run
```

- Runs `mlb_client.init()`, starts Discord (slash commands, MLB Poly views), and an **America/New_York** schedule:
  - **10:00** & **16:00** — full `run_cycle` (games, Polymarket `sports` markets, signals, Discord posts, auto-bet, resolution pass)
  - **00:00** — `check_mlb_resolutions` + resolution embeds
  - **07:00** — MLB Polymarket daily summary embed  
- On startup, runs one `run_cycle` immediately so deploys do not wait until 10am.

```bash
python main.py test
```

Today's games, `get_markets(category="sports")`, signals — **stdout only** (no Discord, no orders).

```bash
python main.py status
```

Polymarket balance (if configured), open `mlb_poly` positions, top pitcher multipliers, top team scores.

```bash
python main.py games
python main.py games 2025-05-17
```

Schedule with probable starters and announce status (`both probables` / `home only` / …).

```bash
python main.py simulate "Yankees" "2025-05-17"
```

Full `calculate_win_probability` JSON for the matching game (requires probables and rest rules).

## Data

- `data/park_factors.json` — run/hr factors indexed to **1.0 = league average** (2024 Statcast-style estimates; Coors ~1.22 runs, Tropicana ~0.88).
- `data/team_timezones.json` — home city, IANA timezone, coordinates, ballpark name.

## Layout

- `pipeline/` — MLB helpers, copied Poly/Discord/order helpers, learning loop.
- `logs/` — positions, signals, scores (seeded empty).
- `config.py` — loads and validates env; **`CONFIG`** dict.
