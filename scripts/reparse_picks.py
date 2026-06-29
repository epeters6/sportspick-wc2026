"""Re-parse raw_text on existing picks with the latest pick_extractor rules."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    from backend.db import get_db
    from backend.scrapers.pick_extractor import extract_all_picks

    db = get_db()
    rows = (
        db.table("picks")
        .select("id, raw_text, platform, post_id, bet_type")
        .eq("outcome", "pending")
        .execute()
        .data or []
    )
    updated = 0
    for row in rows:
        text = row.get("raw_text") or ""
        if not text:
            continue
        picks = extract_all_picks(text)
        if not picks:
            continue
        # Match by post_id suffix when multi-pick
        post_id = row.get("post_id") or ""
        pick = picks[0]
        if "-pick-" in post_id:
            try:
                idx = int(post_id.rsplit("-pick-", 1)[1])
                if idx < len(picks):
                    pick = picks[idx]
            except ValueError:
                pass
        db.table("picks").update({
            "predicted_winner": pick.get("predicted_winner"),
            "bet_type": pick.get("bet_type"),
            "bet_line": pick.get("bet_line"),
            "bet_subject": pick.get("bet_subject"),
            "confidence": pick.get("confidence"),
        }).eq("id", row["id"]).execute()
        updated += 1
    print(f"Re-parsed {updated} pending picks")


if __name__ == "__main__":
    main()
