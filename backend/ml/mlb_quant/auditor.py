import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytz

_QUANT_DIR = Path(__file__).resolve().parent
MODEL_STATE_PATH = _QUANT_DIR / "under_model_state.json"
PENDING_PREDICTIONS_PATH = _QUANT_DIR / "pending_predictions.json"

EASTERN = pytz.timezone("US/Eastern")


def _safe_load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _build_report_text() -> tuple[str, float, int, int]:
    now_et = datetime.now(EASTERN)

    model_state = _safe_load_json(MODEL_STATE_PATH, {})
    pending = _safe_load_json(PENDING_PREDICTIONS_PATH, [])
    if not isinstance(pending, list):
        pending = []

    under_weights = {}
    over_weights = {}
    updates_under = 0
    updates_over = 0
    updates_total = 0
    if isinstance(model_state, dict):
        if isinstance(model_state.get("under_model"), dict):
            under_model = model_state.get("under_model", {})
            over_model = model_state.get("over_model", {})
            under_weights = under_model.get("weights", {}) if isinstance(under_model, dict) else {}
            over_weights = over_model.get("weights", {}) if isinstance(over_model, dict) else {}
            updates_under = int(under_model.get("updates", 0)) if isinstance(under_model, dict) else 0
            updates_over = int(over_model.get("updates", 0)) if isinstance(over_model, dict) else 0
            updates_total = int(model_state.get("combined_updates", updates_under + updates_over))
        else:
            # Legacy single-model payload
            under_weights = model_state.get("weights", {})
            over_weights = {}
            updates_under = int(model_state.get("updates", 0))
            updates_over = 0
            updates_total = updates_under

    def _record_side_correct(rec: dict[str, Any]) -> bool:
        if "predicted_side_correct" in rec:
            return bool(rec.get("predicted_side_correct"))
        predicted_side = str(
            rec.get("predicted_side")
            or ("UNDER" if int(rec.get("predicted_under", 1)) == 1 else "OVER")
        ).upper()
        if predicted_side == "OVER":
            label_over = int(rec.get("label_over", 1 - int(rec.get("label_under", 0))))
            return label_over == 1
        return int(rec.get("label_under", 0)) == 1

    def _record_pred_prob_and_label(rec: dict[str, Any]) -> tuple[float, float] | None:
        if "predicted_proba_before_update" in rec:
            pred = float(rec.get("predicted_proba_before_update", 0.5))
            side = str(
                rec.get("predicted_side")
                or ("UNDER" if int(rec.get("predicted_under", 1)) == 1 else "OVER")
            ).upper()
            label = float(
                rec.get("label_under", 0)
                if side == "UNDER"
                else rec.get("label_over", 1 - int(rec.get("label_under", 0)))
            )
            return pred, label
        if "proba_before_update" in rec:
            return float(rec.get("proba_before_update", 0.5)), float(rec.get("label_under", 0))
        return None

    resolved = [p for p in pending if isinstance(p, dict) and p.get("resolved")]
    unresolved = [p for p in pending if isinstance(p, dict) and not p.get("resolved")]

    wins = int(sum(1 for p in resolved if _record_side_correct(p)))
    total = len(resolved)
    accuracy = (wins / total) if total > 0 else 0.0

    brier_values = []
    for p in resolved:
        pair = _record_pred_prob_and_label(p)
        if pair is not None:
            pred, label = pair
            brier_values.append((pred - label) ** 2)
    brier = float(np.mean(brier_values)) if brier_values else None

    seven_day_cutoff = now_et - timedelta(days=7)
    resolved_7d = []
    for p in resolved:
        resolved_at = _parse_iso_dt(p.get("resolved_at"))
        if resolved_at is not None:
            if resolved_at.tzinfo is None:
                resolved_at = EASTERN.localize(resolved_at)
            resolved_et = resolved_at.astimezone(EASTERN)
            if resolved_et >= seven_day_cutoff:
                resolved_7d.append(p)
    wins_7d = int(sum(1 for p in resolved_7d if _record_side_correct(p)))
    total_7d = len(resolved_7d)
    acc_7d = (wins_7d / total_7d) if total_7d > 0 else 0.0

    under_feature_weights = {
        k: float(v)
        for k, v in under_weights.items()
        if k != "bias"
    } if isinstance(under_weights, dict) else {}
    over_feature_weights = {
        k: float(v)
        for k, v in over_weights.items()
        if k != "bias"
    } if isinstance(over_weights, dict) else {}

    under_top_pos = sorted(under_feature_weights.items(), key=lambda x: x[1], reverse=True)[:5]
    under_top_neg = sorted(under_feature_weights.items(), key=lambda x: x[1])[:5]
    over_top_pos = sorted(over_feature_weights.items(), key=lambda x: x[1], reverse=True)[:5]
    over_top_neg = sorted(over_feature_weights.items(), key=lambda x: x[1])[:5]

    lines = [
        f"Morning ML Report ({now_et.strftime('%Y-%m-%d %I:%M %p ET')})",
        f"Model updates applied (U/O/T): {updates_under}/{updates_over}/{updates_total}",
        f"Resolved predictions: {total} | Hit rate: {accuracy * 100:.2f}% ({wins}/{total})",
        f"7-day hit rate: {acc_7d * 100:.2f}% ({wins_7d}/{total_7d})",
        f"Unresolved queue: {len(unresolved)}",
        f"Brier score: {brier:.4f}" if brier is not None else "Brier score: n/a (insufficient settled probabilities)",
        "",
        "Under model top positive weights:",
    ]
    for key, value in under_top_pos:
        lines.append(f"  + {key}: {value:.4f}")

    lines.append("Under model top negative weights:")
    for key, value in under_top_neg:
        lines.append(f"  - {key}: {value:.4f}")

    if over_feature_weights:
        lines.append("")
        lines.append("Over model top positive weights:")
        for key, value in over_top_pos:
            lines.append(f"  + {key}: {value:.4f}")

        lines.append("Over model top negative weights:")
        for key, value in over_top_neg:
            lines.append(f"  - {key}: {value:.4f}")

    report_text = "\n".join(lines)
    return report_text, accuracy, total, wins


def _send_report(report_text: str, accuracy: float, total: int, wins: int) -> None:
    try:
        from discord_alerts import send_morning_ml_report  # type: ignore

        send_morning_ml_report(report_text)
        print("Morning ML report posted via send_morning_ml_report.")
        return
    except Exception:
        pass

    try:
        from discord_alerts import send_audit_report  # type: ignore

        send_audit_report(round(accuracy * 100, 2), total, wins, report_text)
        print("Morning ML report posted via send_audit_report fallback.")
        return
    except Exception:
        pass

    print("--- Morning ML Report ---")
    print(report_text)


def run_audit() -> None:
    report_text, accuracy, total, wins = _build_report_text()
    _send_report(report_text, accuracy, total, wins)


if __name__ == "__main__":
    run_audit()
