"""Reject delayed external triggers without replaying an earlier decision time.

This gate does not run trading code or change the frozen experiment. The
orchestrator continues to use its actual execution time for every decision.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os


def check_dispatch(kind: str, scheduled_for: str, now: datetime) -> tuple[bool, str]:
    if kind not in {"weather", "clv"}:
        raise ValueError("unknown workflow kind")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("current time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if kind == "weather":
        hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        # The frozen orchestrator captures its hour before its outcome stages.
        # Leave the full workflow timeout so forecasting cannot cross that hour.
        if now + timedelta(minutes=25) >= hour_end:
            return False, "insufficient_time_in_weather_hour"
    if not scheduled_for:
        return True, "manual_or_github_schedule"
    try:
        scheduled = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False, "invalid_dispatch_timestamp"
    if scheduled.tzinfo is None or scheduled.utcoffset() != timedelta(0):
        return False, "dispatch_timestamp_must_be_utc"
    age = now - scheduled
    if age < -timedelta(seconds=60):
        return False, "future_dispatch_timestamp"
    if kind == "weather":
        if scheduled.replace(minute=0, second=0, microsecond=0) != now.replace(minute=0, second=0, microsecond=0):
            return False, "expired_weather_hour"
    elif age > timedelta(minutes=5):
        return False, "expired_clv_dispatch"
    return True, "current_dispatch"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("weather", "clv"))
    args = parser.parse_args()
    allowed, reason = check_dispatch(
        args.kind, os.environ.get("WEATHER_SCHEDULED_FOR", ""), datetime.now(timezone.utc)
    )
    # Never echo arbitrary workflow input into the log or GitHub output file.
    print(f"Dispatch gate: {reason}; execute={str(allowed).lower()}")
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"execute={str(allowed).lower()}\nreason={reason}\n")
    if not allowed:
        print(f"::warning::Weather scheduler dispatch skipped: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
