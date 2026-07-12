# SportsPick Tracker — World Cup 2026 Edition

Track 100+ sports pick influencers across Twitter, TikTok, and Instagram.
Uses an Elo + accuracy ML model to rank them and surface the best consensus picks.

---

## What it does

- **Scrapes** picks from influencers (Covers, YouTube, ActionNetwork, Pickswise, optional Twitter/TikTok) 3× daily
- **Resolves** picks automatically when World Cup / MLB matches finish
- **Ranks** influencers by Elo score (accuracy-weighted) on each ML run
- **Computes consensus** — Elo-weighted vote aggregation blended with quant models (MLB Pavlov engine, WC team-Elo model, weather portfolio optimizer)
- **Shadow-bets** the edges on Polymarket/Kalshi (paper mode), reports daily results to Discord, and gates live promotion on a proven paper track record
- **Dashboard** — a Next.js web app to visualise everything in real time

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  GitHub Actions (free cron scheduling)                               │
│                                                                      │
│  sync_scrape.yml (3×/day)    ──▶  match sync + all pick scrapers    │
│  sync_ml.yml (3×/day)        ──▶  Elo + consensus + quant + autobet │
│  pavlov_mlb.yml (3×/day)     ──▶  Pavlov MLB cycle + resolution     │
│  daily_report.yml (1×/day)   ──▶  shadow results → Discord          │
│  worldcup_sync.yml (manual)  ──▶  WC match results + resolve picks  │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ reads/writes
                           ▼
          ┌────────────────────────────────┐
          │   Supabase (PostgreSQL)        │
          │   influencers · picks          │
          │   matches · consensus_picks    │
          └──────────┬─────────────────────┘
                     │ reads
          ┌──────────▼─────────────────────┐
          │   Hugging Face Spaces (Docker) │
          │   FastAPI REST API :7860       │
          │   /influencers /matches        │
          │   /recommendations /stats      │
          └──────────┬─────────────────────┘
                     │ NEXT_PUBLIC_API_URL
          ┌──────────▼─────────────────────┐
          │   Vercel (Next.js Dashboard)   │
          │   Leaderboard · Matches        │
          │   Recommendations              │
          └────────────────────────────────┘
```

---

## Quick Start

### 1. Create a Supabase project
1. Go to [supabase.com](https://supabase.com) → New project
2. In the SQL editor, run `supabase/migrations/001_initial_schema.sql`
3. Copy your **Project URL**, **anon key**, and **service_role key**

### 2. Configure credentials
```bash
cp .env.example .env
# Edit .env with your keys:
nano .env
```

**Required for World Cup data (free):**
- Sign up at [wc2026api.com](https://wc2026api.com) → free tier → get API key

**Required for Twitter scraping (free, cookie-based):**
1. Log in to twitter.com in your browser
2. Open DevTools → Application → Cookies → `twitter.com`
3. Copy `auth_token` and `ct0` cookie values into `.env`

**For TikTok:** grab your `sessionid` cookie from tiktok.com after logging in

**For Instagram:** just put your username/password in `.env`

### 3. Install and run (local)
```bash
# Python backend
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r backend/requirements.txt
playwright install chromium

# Start API
uvicorn backend.api.main:app --reload

# In another terminal — Next.js dashboard
cd frontend
npm install
npm run dev
```

Visit:
- Dashboard: http://localhost:3000
- API docs: http://localhost:8000/docs

### 4. Seed and first sync
```bash
curl -X POST http://localhost:8000/seed   # adds ~70 curated accounts
curl -X POST http://localhost:8000/sync   # first scrape + WC data
```

---

## Production Deployment (100% Free Stack)

Everything runs for free: GitHub Actions for cron jobs, Hugging Face Spaces for the API, Vercel for the dashboard, and Supabase for the database.

---

### Step 1 — Supabase (database)

1. Go to [supabase.com](https://supabase.com) → **New project**
2. In the SQL editor, run `supabase/migrations/001_initial_schema.sql`
3. Copy your **Project URL**, **anon key**, and **service_role key** — you'll need them in every step below

---

### Step 2 — GitHub (repo + Actions secrets)

1. Push this repo to GitHub: `git push origin main`
2. Go to your repo → **Settings → Secrets and variables → Actions**
3. Add the following **repository secrets**:

| Secret name | Value |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Supabase **anon** key (alias: `SUPABASE_ANON_KEY`) |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase **service_role** key |
| `TWITTER_AUTH_TOKEN` | Twitter `auth_token` cookie |
| `TWITTER_CT0` | Twitter `ct0` cookie |
| `TWITTER_COOKIES` | Full cookie JSON (optional, for twikit) |
| `TIKTOK_USERNAME` | TikTok username |
| `TIKTOK_PASSWORD` | TikTok password |
| `INSTAGRAM_USERNAME` | Instagram username |
| `INSTAGRAM_PASSWORD` | Instagram password |
| `WC_API_KEY` | World Cup 2026 API key |
| `WC_API_BASE` | `https://api.wc2026api.com/v1` |

4. GitHub Actions workflows will start running automatically on their cron schedules:
   - `.github/workflows/sync_scrape.yml` — **09/15/21 UTC** (match sync + pick scrapers)
   - `.github/workflows/sync_ml.yml` — **10/16/22 UTC** (Elo, consensus, quant models, autobet, shadow validation)
   - `.github/workflows/pavlov_mlb.yml` — **14/20 UTC** pregame + **05 UTC** resolution
   - `.github/workflows/daily_report.yml` — **12 UTC** (yesterday's shadow results → Discord)
   - `.github/workflows/worldcup_sync.yml` — manual dispatch (WC results + resolve picks)

> You can trigger any workflow manually via the **Actions** tab → select workflow → **Run workflow**.

---

### Step 3 — Hugging Face Spaces (FastAPI API)

1. Sign up at [huggingface.co](https://huggingface.co) (free)
2. Click **New Space** → name it `sportspick-tracker-api`
3. Choose **Docker** SDK
4. In **Space Settings → Variables**, add:
   - `SUPABASE_URL` — your project URL
   - `SUPABASE_ANON_KEY` — anon key
   - `SUPABASE_SERVICE_ROLE_KEY` — service role key
   - `WC_API_KEY` — WC API key
   - `APP_ENV` — `production`
5. Push the Space contents — copy the files from `huggingface/` to the Space repo root:

```bash
# One-time: clone your HF Space and add the API files
git clone https://huggingface.co/spaces/YOUR_HF_USERNAME/sportspick-tracker-api hf-space
cp huggingface/Dockerfile        hf-space/Dockerfile
cp huggingface/requirements-hf.txt hf-space/requirements-hf.txt
cp huggingface/main_hf.py        hf-space/main_hf.py
cp -r backend/                   hf-space/backend/
cd hf-space && git add . && git commit -m "deploy api" && git push
```

6. HF Spaces will build the Docker image and start the API on port 7860
7. Note your Space URL: `https://YOUR_HF_USERNAME-sportspick-tracker-api.hf.space`

---

### Step 4 — Vercel (Next.js dashboard)

1. Go to [vercel.com](https://vercel.com) → **Add New Project** → import your GitHub repo
2. Set the **Root Directory** to `frontend`
3. Add this **Environment Variable** in Vercel's project settings:

   | Key | Value |
   |---|---|
   | `NEXT_PUBLIC_API_URL` | `https://YOUR_HF_USERNAME-sportspick-tracker-api.hf.space` |

4. Click **Deploy** — Vercel will build and host the dashboard automatically
5. Update `frontend/vercel.json` with your actual HF Space URL

> Every `git push` to `main` will redeploy the dashboard automatically.

---

### Step 5 — Seed the database (one-time)

Once all three services are deployed, seed the influencer list:

```bash
curl -X POST https://YOUR_HF_USERNAME-sportspick-tracker-api.hf.space/seed
```

This populates ~70 curated sports pick accounts. The cron workflows will pick up from there.

---

## ML Model: How ranking works

Each influencer starts at **Elo 1000**. For every resolved pick:
- **Correct pick** → Elo increases (K=32, scaled by recency weight)
- **Incorrect pick** → Elo decreases
- Picks older than 30 days are down-weighted (half-life decay)

**Consensus picks** are computed by having each influencer cast an Elo-weighted vote for their predicted winner. The result is the confidence score shown on the dashboard.

---

## Adding more influencers

Edit the seed lists in each scraper file:
- `backend/scrapers/twitter_scraper.py` → `TOP_TWITTER_SPORTS_ACCOUNTS`
- `backend/scrapers/tiktok_scraper.py` → `TOP_TIKTOK_SPORTS_ACCOUNTS`
- `backend/scrapers/instagram_scraper.py` → `TOP_INSTAGRAM_SPORTS_ACCOUNTS`

Or call the API directly:
```bash
curl -X POST http://localhost:8000/seed
```

You can also add influencers directly in Supabase's table editor.

---

## Expanding beyond World Cup

The schema is sport-agnostic. The `sport` column on `matches` already supports:
`football`, `basketball`, `baseball`, `nfl`, `nhl`, `stocks`

To add a new sport:
1. Add a new data fetcher in `backend/sports_data/`
2. Schedule it in `backend/scheduler.py`
3. Add sport-specific keywords to `backend/scrapers/pick_extractor.py`

---

## Project structure

```
Scraper/
├── .github/
│   └── workflows/
│       ├── sync_scrape.yml        Cron 3×/day: match sync + pick scrapers
│       ├── sync_ml.yml            Cron 3×/day: ML + quant + autobet + shadow validation
│       ├── pavlov_mlb.yml         Cron 3×/day: Pavlov MLB cycle + midnight resolution
│       ├── daily_report.yml       Cron 1×/day: shadow results → Discord
│       └── worldcup_sync.yml      Manual: WC results + resolve picks
├── backend/
│   ├── scrapers/
│   │   ├── twitter_scraper.py     Twitter/X via twikit (cookie auth)
│   │   ├── tiktok_scraper.py      TikTok via unofficial API + Playwright
│   │   ├── instagram_scraper.py   Instagram via Instaloader
│   │   └── pick_extractor.py      NLP pick parser (rule-based + regex)
│   ├── ml/
│   │   ├── elo_ranker.py          Elo scoring engine
│   │   ├── consensus_engine.py    Weighted vote aggregation
│   │   └── accuracy_scorer.py     Streaks, leaderboard, consensus scores
│   ├── sports_data/
│   │   └── worldcup_fetcher.py    WC 2026 match data (wc2026api + fallback)
│   ├── api/
│   │   └── main.py                FastAPI REST API
│   ├── scheduler.py               APScheduler (local dev only)
│   ├── config.py                  Pydantic settings
│   └── db.py                      Supabase client
├── frontend/
│   ├── app/
│   │   ├── page.tsx               Dashboard home
│   │   ├── leaderboard/           Influencer leaderboard + detail pages
│   │   ├── matches/               Match list + match detail + pick breakdown
│   │   └── recommendations/       Top consensus picks
│   ├── components/
│   │   ├── Sidebar.tsx
│   │   ├── StatCard.tsx
│   │   ├── PlatformBadge.tsx
│   │   └── ConfidenceBar.tsx
│   ├── lib/api.ts                 Typed API client
│   └── vercel.json                Vercel deployment config
├── huggingface/
│   ├── Dockerfile                 Docker config for HF Spaces (port 7860)
│   ├── main_hf.py                 FastAPI entrypoint (no scheduler)
│   ├── requirements-hf.txt        Trimmed deps (no scrapers/Playwright)
│   └── README.md                  HF Spaces metadata header
├── supabase/
│   └── migrations/001_initial_schema.sql
├── scripts/
│   ├── setup.sh                   Local dev setup script
│   ├── start.sh                   Start both services locally
│   └── seed_and_sync.sh           Initial seed + sync
└── .env.example
```
