# pavlov-weather-bot

An automated weather prediction trading bot that:

1. Fetches open weather markets from [Kalshi](https://kalshi.com).
2. Pulls NWS (NOAA) forecast probabilities for each market's location.
3. Calculates the edge between the NWS forecast and the Kalshi market price.
4. Places Kelly-sized limit orders when the edge exceeds your threshold.
5. Posts signal alerts and position updates to a Discord channel.
6. Tracks station forecast accuracy over time via a learning loop.

---

## Directory Structure

```
pavlov-weather-bot/
├── pipeline/
│   ├── __init__.py         # Package docstring
│   ├── kalshi_client.py    # Kalshi REST API (auth, markets, orders)
│   ├── nws_client.py       # NOAA/NWS REST API (forecasts, observations)
│   ├── station_mapper.py   # Maps market tickers → NWS stations
│   ├── signal_engine.py    # Edge calculation & signal generation
│   ├── discord_bot.py      # Discord alerts & commands
│   ├── order_manager.py    # Kelly sizing, order placement, position log
│   └── learning_loop.py    # Station accuracy scoring
├── data/
│   ├── stations.json       # Ticker → station mapping (seeded automatically)
│   ├── market_cache.json   # Optional market data cache
│   └── forecast_cache.json # Optional forecast data cache
├── logs/
│   ├── positions.json      # All open and closed positions
│   ├── signals.json        # All generated signals
│   └── station_scores.json # Per-station forecast accuracy
├── config.py               # .env loader & validator
├── main.py                 # Entry point & scheduler
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
└── README.md               # This file
```

---

## Prerequisites

- Python 3.10+
- A [Kalshi](https://kalshi.com) account (real-money or demo)
- A Discord bot and server channel

---

## Step 1 – Create a Kalshi Account

1. Visit [https://kalshi.com](https://kalshi.com) and sign up.
2. Complete identity verification.
3. Note your **email** and **password** – these are used by the bot to
   authenticate via the Kalshi REST API.
4. Fund your account (or use a demo account for paper trading).

> **Note:** The bot uses the Kalshi Trading API v2. Review Kalshi's
> [Terms of Service](https://kalshi.com/legal/terms-of-service) and
> [API documentation](https://trading-api.readme.io/reference/getting-started)
> before running with real funds.

---

## Step 2 – Create a Discord Bot

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications).
2. Click **New Application** → give it a name (e.g. `pavlov-weather-bot`).
3. Go to **Bot** → click **Add Bot** → confirm.
4. Under **Token**, click **Reset Token** and copy it. This is your
   `DISCORD_BOT_TOKEN`.
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
7. Copy the generated URL, paste it in your browser, and invite the bot to
   your server.

---

## Step 3 – Get your Discord Channel ID

1. In Discord, go to **User Settings → Advanced** and enable
   **Developer Mode**.
2. Right-click the channel where you want the bot to post alerts.
3. Click **Copy Channel ID**. This is your `DISCORD_CHANNEL_ID`.

---

## Step 4 – Configure Environment Variables

```bash
# Copy the template
cp .env.example .env
```

Edit `.env`:

```dotenv
KALSHI_EMAIL=you@example.com
KALSHI_PASSWORD=your-kalshi-password

DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_CHANNEL_ID=123456789012345678

# Minimum edge required to place a trade (0.15 = 15 cents per dollar)
MIN_EDGE_THRESHOLD=0.15

# Fraction of full Kelly to stake (0.25 = quarter-Kelly, recommended)
KELLY_FRACTION=0.25

# How often to scan markets (minutes)
CHECK_INTERVAL_MINUTES=30
```

---

## Step 5 – Install Dependencies

```bash
cd pavlov-weather-bot
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Step 6 – Run the Bot

```bash
python main.py
```

On startup the bot will:
- Validate all environment variables (exits with a clear error if any are missing).
- Connect the Discord bot and post an "online" message.
- Run the first pipeline cycle immediately.
- Schedule subsequent cycles every `CHECK_INTERVAL_MINUTES`.

---

## What Each File Does

| File | Purpose |
|------|---------|
| `config.py` | Loads and validates all `.env` variables; exports `CONFIG` dict |
| `main.py` | Entry point; initialises components and runs the `schedule` loop |
| `pipeline/kalshi_client.py` | Authenticates with Kalshi; fetches markets; places orders |
| `pipeline/nws_client.py` | Calls NOAA NWS API for hourly forecasts and precipitation probability |
| `pipeline/station_mapper.py` | Maps Kalshi ticker prefixes to NWS station IDs and coordinates |
| `pipeline/signal_engine.py` | Computes edge = forecast_prob − market_price; emits `Signal` objects |
| `pipeline/order_manager.py` | Sizes orders with Kelly criterion; posts to Kalshi; logs positions |
| `pipeline/discord_bot.py` | Posts signal embeds to Discord; handles `!status`, `!scores`, `!signals` |
| `pipeline/learning_loop.py` | Tracks per-station win/loss rate; updates `logs/station_scores.json` |
| `data/stations.json` | Ticker-prefix → station mapping (auto-seeded with 9 US cities) |
| `logs/positions.json` | Persistent list of all positions (open and closed) |
| `logs/signals.json` | Persistent list of all generated signals |
| `logs/station_scores.json` | Per-station forecast accuracy counters |

---

## Discord Commands

| Command | Description |
|---------|-------------|
| `!status` | Shows open positions |
| `!scores` | Shows per-station forecast accuracy |
| `!signals` | Shows the last 10 generated signals |
| `!help_bot` | Lists all available commands |

---

## Adding New Markets / Stations

Edit (or let the bot auto-extend) `data/stations.json`:

```json
[
  {
    "series_prefix": "KXRAIN-PHX",
    "station_id": "KPHX",
    "city": "Phoenix",
    "lat": 33.4373,
    "lon": -112.0078
  }
]
```

Or call `StationMapper.add_station(entry)` programmatically from `main.py`.

---

## Risk Disclaimer

This software is for **educational and research purposes only**.
Weather prediction markets involve real financial risk.
Quarter-Kelly sizing is conservative but losses are still possible.
Always paper-trade first. Never risk money you cannot afford to lose.
