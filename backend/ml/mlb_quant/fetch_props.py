import json
import os
import unicodedata
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import requests

_QUANT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = _QUANT_DIR / "manifest.json"
API_KEY = os.getenv("THE_ODDS_API_KEY", "")
SPORT = "baseball_mlb"
MARKET = "pitcher_outs"
BOOK_ALLOWLIST = {"draftkings", "fanduel", "betmgm"}


def _atomic_write_json(path: Path, payload: Any) -> None:
    try:
        from backend.db import get_db
        db = get_db()
        from datetime import datetime
        db.table("mlb_model_state").upsert({
            "state_key": "manifest",
            "state_value": payload,
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="state_key").execute()
    except Exception as e:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
        os.replace(tmp_path, path)


def _normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    chars = []
    for ch in text:
        chars.append(ch if (ch.isalnum() or ch == " ") else " ")
    return " ".join("".join(chars).split())


def _safe_get_json(url: str) -> Any:
    resp = requests.get(url, timeout=8)
    resp.raise_for_status()
    return resp.json()


def update_manifest_with_props() -> None:
    manifest = {}
    try:
        from backend.db import get_db
        db = get_db()
        res = db.table("mlb_model_state").select("state_value").eq("state_key", "manifest").execute()
        if res.data:
            manifest = res.data[0]["state_value"]
    except Exception as e:
        print(f"Fallback to local manifest: {e}")
        if MANIFEST_PATH.exists():
            try:
                with MANIFEST_PATH.open("r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                pass

    if not isinstance(manifest, dict):
        raise ValueError("manifest.json is invalid.")

    pitcher_name_index: dict[str, str] = {}
    for pid, payload in manifest.items():
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        if not name:
            continue
        pitcher_name_index[_normalize_name(name)] = str(pid)

    events_url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/events?apiKey={API_KEY}"
    events = _safe_get_json(events_url)
    if not isinstance(events, list):
        print("No events payload received from Odds API.")
        return

    matched_points: dict[str, list[float]] = {}
    for event in events:
        eid = event.get("id")
        if not eid:
            continue

        props_url = (
            f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{eid}/odds"
            f"?apiKey={API_KEY}&regions=us&markets={MARKET}"
        )
        try:
            odds_data = _safe_get_json(props_url)
        except Exception as exc:
            print(f"Failed fetching props for event={eid}: {exc}")
            continue

        for book in odds_data.get("bookmakers", []):
            if book.get("key") not in BOOK_ALLOWLIST:
                continue
            for market in book.get("markets", []):
                if market.get("key") != MARKET:
                    continue
                for outcome in market.get("outcomes", []):
                    desc = outcome.get("description")
                    point = outcome.get("point")
                    if desc is None or point is None:
                        continue

                    normalized_desc = _normalize_name(desc)
                    matched_pid = None

                    if normalized_desc in pitcher_name_index:
                        matched_pid = pitcher_name_index[normalized_desc]
                    else:
                        for normalized_pitcher, pid in pitcher_name_index.items():
                            if normalized_pitcher in normalized_desc or normalized_desc in normalized_pitcher:
                                matched_pid = pid
                                break

                    if matched_pid is None:
                        continue
                    matched_points.setdefault(matched_pid, []).append(float(point))

    updated = 0
    for pid, points in matched_points.items():
        if pid not in manifest or not isinstance(manifest[pid], dict):
            continue
        payload = manifest[pid]
        consolidated_line = round(float(median(points)), 1)
        previous_line = float(payload.get("prop_line", consolidated_line))

        line_ctx = payload.get("line_movement", {})
        if not isinstance(line_ctx, dict):
            line_ctx = {}
        opening_line = float(line_ctx.get("opening_line", previous_line))
        line_move_delta = round(consolidated_line - opening_line, 2)
        last_move_delta = round(consolidated_line - previous_line, 2)

        line_ctx.update(
            {
                "opening_line": round(opening_line, 1),
                "previous_line": round(previous_line, 1),
                "current_line": round(consolidated_line, 1),
                "line_move_delta": line_move_delta,
                "line_move_abs": round(abs(line_move_delta), 2),
                "last_move_delta": last_move_delta,
                "book_count": len(points),
                "min_book_line": round(min(points), 1),
                "max_book_line": round(max(points), 1),
                "line_last_updated_utc": datetime.utcnow().isoformat(),
            }
        )

        payload["prop_line"] = consolidated_line
        payload["line_movement"] = line_ctx
        updated += 1
        print(
            f"Set {payload.get('name', pid)} to {consolidated_line} outs "
            f"(open {opening_line:.1f}, move {line_move_delta:+.1f})"
        )

    _atomic_write_json(MANIFEST_PATH, manifest)
    print(f"Updated prop lines for {updated} pitchers.")


if __name__ == "__main__":
    update_manifest_with_props()
