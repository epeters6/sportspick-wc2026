"""
Run one Pavlov MLB pregame cycle with Supabase state sync.

Scheduled at 10:00 ET and 16:00 ET via GitHub Actions (see pavlov_mlb.yml).
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAVLOV = os.path.join(ROOT, "pavlov")
MLB = os.path.join(PAVLOV, "pavlov-mlb-bot")


def main() -> int:
    sys.path.insert(0, ROOT)
    from backend.pavlov_state import default_mlb_state_dir, pull_mlb, push_mlb

    state_dir = default_mlb_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    pull_mlb(state_dir)

    env = os.environ.copy()
    env["STATE_DIRECTORY"] = str(state_dir)
    env["PYTHONPATH"] = PAVLOV + os.pathsep + MLB + os.pathsep + env.get("PYTHONPATH", "")

    print(f"=== Pavlov MLB cycle-once (state={state_dir}) ===")
    result = subprocess.run(
        [sys.executable, "main.py", "cycle-once"],
        cwd=MLB,
        env=env,
    )

    push_mlb(state_dir)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
