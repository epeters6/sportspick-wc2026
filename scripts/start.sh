#!/usr/bin/env bash
# ─── SportsPick Tracker — Local development launcher ─────────────────────────
# Starts the FastAPI backend and Next.js frontend in separate terminal windows.
# For production, see the GitHub Actions + HF Spaces + Vercel setup in README.md.
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Activate Python venv if present
if [ -f "$REPO_ROOT/venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/venv/bin/activate"
fi

echo "=== Starting SportsPick Tracker (local dev) ==="
echo ""

# ── Backend (FastAPI) ─────────────────────────────────────────────────────────
echo "→ Starting FastAPI backend on http://localhost:8000 ..."
cd "$REPO_ROOT"

if command -v gnome-terminal &>/dev/null; then
  gnome-terminal -- bash -c "uvicorn backend.api.main:app --reload; exec bash" &
elif command -v osascript &>/dev/null; then
  # macOS
  osascript -e "tell application \"Terminal\" to do script \"cd '$REPO_ROOT' && source venv/bin/activate && uvicorn backend.api.main:app --reload\""
elif command -v tmux &>/dev/null; then
  tmux new-session -d -s sportspick-backend 2>/dev/null || true
  tmux send-keys -t sportspick-backend "cd '$REPO_ROOT' && source venv/bin/activate && uvicorn backend.api.main:app --reload" Enter
  echo "   Backend running in tmux session 'sportspick-backend'"
  echo "   View with: tmux attach -t sportspick-backend"
else
  echo "   Run in a separate terminal:"
  echo "   cd '$REPO_ROOT' && source venv/bin/activate && uvicorn backend.api.main:app --reload"
fi

# ── Frontend (Next.js) ────────────────────────────────────────────────────────
echo "→ Starting Next.js frontend on http://localhost:3000 ..."
cd "$REPO_ROOT/frontend"

if command -v gnome-terminal &>/dev/null; then
  gnome-terminal -- bash -c "npm run dev; exec bash" &
elif command -v osascript &>/dev/null; then
  osascript -e "tell application \"Terminal\" to do script \"cd '$REPO_ROOT/frontend' && npm run dev\""
elif command -v tmux &>/dev/null; then
  tmux new-session -d -s sportspick-frontend 2>/dev/null || true
  tmux send-keys -t sportspick-frontend "cd '$REPO_ROOT/frontend' && npm run dev" Enter
  echo "   Frontend running in tmux session 'sportspick-frontend'"
  echo "   View with: tmux attach -t sportspick-frontend"
else
  echo "   Run in a separate terminal:"
  echo "   cd '$REPO_ROOT/frontend' && npm run dev"
fi

echo ""
echo "✅ SportsPick Tracker (local dev)"
echo "   Dashboard: http://localhost:3000"
echo "   API:       http://localhost:8000"
echo "   API docs:  http://localhost:8000/docs"
