---
title: SportsPick Tracker API
emoji: 🏆
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
python_version: "3.11"
pinned: false
license: mit
short_description: FastAPI ML backend — sports pick influencer rankings & consensus
---

# SportsPick Tracker — FastAPI Backend

This Hugging Face Space runs the **SportsPick Tracker** FastAPI backend.

It exposes the REST API for the Next.js dashboard deployed on Vercel.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/influencers` | Influencer leaderboard (Elo-ranked) |
| GET | `/influencers/{id}` | Single influencer + recent picks |
| GET | `/matches` | Upcoming & recent World Cup matches |
| GET | `/matches/{id}/picks` | All picks for a match |
| GET | `/recommendations` | Top consensus picks |
| GET | `/stats/overview` | Summary stats |
| POST | `/seed` | Seed influencer accounts (run once) |
| POST | `/sync` | Manually trigger a scrape + sync cycle |

Interactive docs: `https://<your-space-url>/docs`

## Environment Variables (set in Space Settings → Variables)

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (server-side only) |
| `WC_API_KEY` | World Cup 2026 API key |
| `WC_API_BASE` | WC API base URL (default: `https://api.wc2026api.com/v1`) |
| `APP_ENV` | `production` |
| `LOG_LEVEL` | `INFO` |

> **Scraper credentials** (Twitter, TikTok, Instagram) are **not** needed here —
> scraping is handled exclusively by GitHub Actions workflows.

## Architecture

```
GitHub Actions (cron)          Hugging Face Space (Docker)
┌──────────────────────┐       ┌────────────────────────────┐
│  scrape.yml (30 min) │──────▶│  FastAPI :7860             │
│  ml_sync.yml (1 hr)  │  DB   │  - /influencers            │
│  worldcup_sync.yml   │◀─────▶│  - /matches                │
│      (15 min)        │       │  - /recommendations        │
└──────────────────────┘       └────────────┬───────────────┘
                                            │
                       Supabase (PostgreSQL)│
                       ◀───────────────────▶│
                                            │
                              Vercel (Next.js dashboard)
                              ──────────────▶ NEXT_PUBLIC_API_URL
```
