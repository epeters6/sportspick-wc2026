"""
Run Pavlov MLB midnight resolution with Supabase state sync.
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAVLOV = os.path.join(ROOT, "pavlov")
MLB = os.path.join(PAVLOV, "pavlov-mlb-bot")


def main() -> int:
    sys.path.insert(0, ROOT)
    sys.path.insert(0, PAVLOV)
    sys.path.insert(0, MLB)

    from backend.pavlov_state import default_mlb_state_dir, pull_mlb, push_mlb

    state_dir = default_mlb_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["STATE_DIRECTORY"] = str(state_dir)

    pull_mlb(state_dir)

    from main import run_midnight_resolution

    print(f"=== Pavlov MLB resolve (state={state_dir}) ===")
    asyncio.run(run_midnight_resolution())

    push_mlb(state_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
