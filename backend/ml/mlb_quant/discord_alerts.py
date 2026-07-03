import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import requests

WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1496207790975221883/p8JdTM1Ibz6YdmWhwicmuDoSxnwJsBx4hU_goeJ4QWGlIN1pc8eA_CEDU8kYBxiY0YHb",
)
REQUEST_TIMEOUT_SECONDS = 8


def _clip(text: Any, limit: int = 900) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value if value else "-"
    return value[: max(0, limit - 1)] + "..."


def _safe_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "n/a"


def _parse_reason_lines(reason: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in str(reason or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _side_color(side: str) -> int:
    s = str(side or "").upper()
    if s == "OVER":
        return 3066993
    return 15158332


def send_to_discord(payload: dict[str, Any]) -> int:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not configured.")
        return 0

    try:
        response = requests.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 300:
            print(f"Discord webhook returned status={response.status_code} body={response.text[:300]}")
        return int(response.status_code)
    except Exception as exc:
        print(f"Discord webhook send failed: {exc}")
        return 0


def send_morning_slate(manifest: dict[str, Any]) -> int:
    fields = []
    for _p_id, data in manifest.items():
        name = data.get("name", "Unknown")
        line = data.get("prop_line", "?")
        tier = data.get("tier", "?")
        bullpen = data.get("bullpen_status", "n/a")
        fields.append(
            {
                "name": f"{name} (T{tier})",
                "value": f"Line: **{line} Outs** | Bullpen: {bullpen}",
                "inline": True,
            }
        )

    payload = {
        "content": "Calendar Daily target slate loaded.",
        "embeds": [
            {
                "title": "MLB Outs Engine - Morning Slate",
                "description": "Dual-direction monitor active. Tracking live drift and hook risk.",
                "color": 3447003,
                "fields": fields[:25],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return send_to_discord(payload)


def send_lock_in_ping(
    name: str,
    prop: str,
    line: float,
    reason: str,
    side: str = "UNDER",
    under_proba: Optional[float] = None,
    over_proba: Optional[float] = None,
    model_updates_under: Optional[int] = None,
    model_updates_over: Optional[int] = None,
    mode: Optional[str] = None,
) -> int:
    side_norm = str(side or "UNDER").upper()
    if side_norm not in {"UNDER", "OVER"}:
        side_norm = "UNDER"

    parsed = _parse_reason_lines(reason)
    edge_gap = None
    if under_proba is not None and over_proba is not None:
        edge_gap = abs(float(under_proba) - float(over_proba))

    selected_confidence = under_proba if side_norm == "UNDER" else over_proba
    action_text = "BET THE OVER" if side_norm == "OVER" else "BET THE UNDER"
    side_emoji = "UPTREND" if side_norm == "OVER" else "DOWNTREND"

    fields = [
        {"name": "Prop", "value": _clip(prop, 80), "inline": True},
        {"name": "Side", "value": side_norm, "inline": True},
        {"name": "Line @ Alert", "value": str(line), "inline": True},
        {
            "name": "Model Confidence",
            "value": f"{_safe_pct(selected_confidence)} ({side_norm})",
            "inline": True,
        },
        {
            "name": "Edge Gap",
            "value": _safe_pct(edge_gap),
            "inline": True,
        },
        {
            "name": "Model Updates (U/O)",
            "value": f"{model_updates_under if model_updates_under is not None else 'n/a'}/"
            f"{model_updates_over if model_updates_over is not None else 'n/a'}",
            "inline": True,
        },
    ]

    for key in ("Lock Trigger", "Outs", "Pitch Count", "Run Diff", "Recovery Posterior"):
        if key in parsed:
            fields.append(
                {
                    "name": key,
                    "value": _clip(parsed[key], 180),
                    "inline": key in {"Lock Trigger", "Outs", "Pitch Count", "Run Diff"},
                }
            )

    fields.append(
        {
            "name": "Signal Context",
            "value": f"```{_clip(reason, 950)}```",
            "inline": False,
        }
    )

    payload = {
        "content": f"{side_emoji} LOCK-IN SIGNAL: {name} -> {side_norm}",
        "embeds": [
            {
                "title": "Directional Outs Signal Triggered",
                "description": (
                    f"Action: **{action_text}**\n"
                    f"Mode: `{_clip(mode, 20) if mode else 'n/a'}`"
                ),
                "color": _side_color(side_norm),
                "fields": fields[:25],
                "footer": {"text": f"MLB Quant Engine v4.1 | {action_text}"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return send_to_discord(payload)


def send_audit_report(accuracy: float, total: int, correct: int, details: str) -> int:
    payload = {
        "content": "Bar chart Post-game audit report",
        "embeds": [
            {
                "title": f"Daily Performance: {accuracy}% Accuracy",
                "description": f"Predicted {correct} out of {total} events.",
                "color": 3066993 if accuracy >= 56 else 15105570,
                "fields": [
                    {"name": "Detailed Breakdown", "value": _clip(details, 3500), "inline": False}
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return send_to_discord(payload)


def send_morning_ml_report(report_text: str) -> int:
    payload = {
        "content": "Sunrise Morning ML report",
        "embeds": [
            {
                "title": "Model Health and Learning Progress",
                "description": f"```{_clip(report_text, 3900)}```",
                "color": 3447003,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return send_to_discord(payload)

