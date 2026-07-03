"""
Run one Pavlov weather cycle (Kalshi + Polymarket US) with Supabase state sync.

Used by GitHub Actions instead of Railway always-on worker.
Set DISCORD_WEBHOOK_URL to post into the same channel as SportsPick consensus alerts.
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAVLOV = os.path.join(ROOT, "pavlov")


def main() -> int:
    sys.path.insert(0, ROOT)
    from backend.pavlov_state import default_weather_state_dir, pull_weather, push_weather

    state_dir = default_weather_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    pull_weather(state_dir)

    env = os.environ.copy()
    env["STATE_DIRECTORY"] = str(state_dir)
    env["PYTHONPATH"] = PAVLOV + os.pathsep + env.get("PYTHONPATH", "")

    print(f"=== Pavlov weather cycle-once (state={state_dir}) ===")
    result = subprocess.run(
        [sys.executable, "main.py", "cycle-once"],
        cwd=PAVLOV,
        env=env,
    )

    push_weather(state_dir)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
