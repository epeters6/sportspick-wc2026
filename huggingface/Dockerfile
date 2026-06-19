# ─── SportsPick Tracker — FastAPI on Hugging Face Spaces (Docker SDK) ──────────
# HF Spaces requires the app to listen on port 7860.
# Scrapers are NOT included — they run via GitHub Actions cron workflows.
# Only the API + ML modules are bundled here.

FROM python:3.11-slim

# HF Spaces runs containers as a non-root user (uid 1000)
RUN useradd -m -u 1000 user
WORKDIR /app

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps (API + ML only — no scraper / Playwright heavyweights) ───────
COPY requirements-hf.txt .
RUN pip install --no-cache-dir -r requirements-hf.txt

# ── Application code ─────────────────────────────────────────────────────────
# Core backend packages required at runtime
COPY backend/__init__.py           backend/__init__.py
COPY backend/config.py             backend/config.py
COPY backend/db.py                 backend/db.py
COPY backend/api/                  backend/api/
COPY backend/ml/                   backend/ml/
COPY backend/sports_data/          backend/sports_data/

# HF entrypoint — runs the API without APScheduler
COPY main_hf.py                    main_hf.py

RUN chown -R user:user /app
USER user

# ── HF Spaces listens on port 7860 ───────────────────────────────────────────
EXPOSE 7860

ENV PORT=7860 \
    HOST=0.0.0.0 \
    APP_ENV=production \
    LOG_LEVEL=INFO

CMD ["uvicorn", "main_hf:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "2"]
