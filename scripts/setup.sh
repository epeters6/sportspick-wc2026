#!/usr/bin/env bash
# ─── SportsPick Tracker — Local development setup ────────────────────────────
# Run this once to set up your local dev environment.
# Production deployment uses GitHub Actions + Hugging Face Spaces + Vercel.
set -e

echo "=== SportsPick Tracker — Local Dev Setup ==="

# ── 1. Python virtual environment ────────────────────────────────────────────
echo ""
echo "→ Setting up Python virtual environment..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate

pip install --upgrade pip
pip install -r backend/requirements.txt

# ── 2. Playwright browser (required for TikTok scraper) ──────────────────────
echo ""
echo "→ Installing Playwright Chromium..."
playwright install chromium --with-deps

# ── 3. spaCy NLP model ───────────────────────────────────────────────────────
echo ""
echo "→ Downloading spaCy English model..."
python -m spacy download en_core_web_sm

# ── 4. Frontend dependencies ─────────────────────────────────────────────────
echo ""
echo "→ Installing Next.js dependencies..."
cd frontend
npm install
cd ..

# ── 5. Environment file ───────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "⚠️  .env created from .env.example"
  echo "    Fill in your credentials before running the dev server:"
  echo "    SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY"
  echo "    TWITTER_AUTH_TOKEN, TWITTER_CT0"
  echo "    TIKTOK_SESSION_ID (or TIKTOK_USERNAME / TIKTOK_PASSWORD)"
  echo "    INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD"
  echo "    WC_API_KEY"
fi

echo ""
echo "✅ Local setup complete!"
echo ""
echo "Next steps:"
echo "  1. Fill in .env with your credentials"
echo "  2. Apply the Supabase schema:"
echo "     psql \$DATABASE_URL < supabase/migrations/001_initial_schema.sql"
echo "  3. Start local services:"
echo "     # Backend (FastAPI):"
echo "     source venv/bin/activate && uvicorn backend.api.main:app --reload"
echo ""
echo "     # Frontend (Next.js) — in a separate terminal:"
echo "     cd frontend && npm run dev"
echo ""
echo "     # Or use the start script:"
echo "     bash scripts/start.sh"
echo ""
echo "  4. Seed & first sync:"
echo "     curl -X POST http://localhost:8000/seed"
echo "     curl -X POST http://localhost:8000/sync"
echo ""
echo "Production deployment:"
echo "  • Scrapers  → GitHub Actions (cron via .github/workflows/)"
echo "  • API       → Hugging Face Spaces (huggingface/)"
echo "  • Dashboard → Vercel (frontend/)"
echo "  • Database  → Supabase (already configured)"
