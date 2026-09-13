"""Descriptive scorecards from completed weather reports; no I/O or trading imports.

Accounting and evaluation are separate reads, so differences are disclosed rather
than reconciled here. Forecast counters cover one arm's latest cycle, may overlap,
and cannot be assigned to individual venues from the available report.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html
import math
import re

VERSION = "weather_performance_scorecard_v1"
STALE_AFTER_SECONDS = 2 * 60 * 60
VENUES = ("kalshi", "polymarket")
CRITERIA = {
    "reads_complete": "All evaluation reads complete",
    "no_quarantined_evidence": "No quarantined evidence",
    "minimum_independent_traded_events": "Minimum verified traded events",
    "minimum_distinct_traded_dates": "Minimum verified traded dates",
    "positive_lower_95pct_day_cluster_net_roi": "Positive lower 95% day-cluster ROI bound",
    "positive_net_closing_clv": "Positive mean net closing CLV",
    "closing_clv_event_coverage": "Closing CLV on at least 80% of events and 30 complete events",
    "immutable_traded_decisions_verified": "Immutable traded decisions verified",
}
COUNTERS = (
    "polymarket_markets", "kalshi_markets", "events", "ensemble_ok", "ensemble_fail",
    "evaluation_rows", "bets_placed", "observation_only", "kelly_reject", "depth_reject",
    "optimizer_reject", "book_refresh_reject", "normalization_reject", "timing_reject",
    "persistence_reject", "skipped_past", "scope_skipped",
)
READ_ERRORS = {"PREDICTION_READ_FAILED", "BET_READ_FAILED", "CLV_READ_FAILED"}
STAGES = {"experiment", "settlement_before", "verification", "legacy_official_labels", "forecast",
          "forecast_window", "forecast_slot", "settlement_after", "forward_official_labels",
          "forward_evaluation", "accounting", "clv_recovery", "research_arm", "research_arms", "persist_report"}
REJECTION_LABELS = {
    "UNSUPPORTED_SETTLEMENT_STATION": "Station not supported",
    "UNVERIFIED_OBSERVATION_DAY": "Observation day not verified",
    "MISSING_EXPLICIT_SETTLEMENT_STATION": "Contract station not stated explicitly",
    "OUTSIDE_WEATHER_DECISION_WINDOW": "Outside fixed decision hours",
    "INVALID_WEATHER_BUCKET_PARTITION": "Invalid contract bucket ranges",
    "NO_BUCKET_MEETS_MIN_NET_EDGE": "No contract meets minimum net edge",
    "WEATHER_EXECUTION_COHORT_NOT_PROFITABLE": "Historical comparison group failed profitability filter",
    "INSUFFICIENT_HISTORICAL_COHORT": "Too few comparable historical observations",
}
COUNTER_LABELS = {"polymarket_markets": "US markets", "kalshi_markets": "Kalshi markets", "events": "Event groups",
                  "bets_placed": "Paper fills", "ensemble_ok": "Forecast groups", "ensemble_fail": "Forecast failures",
                  "kelly_reject": "General rejects (overlapping)", "scope_skipped": "Outside scope",
                  "evaluation_rows": "Forecast rows", "observation_only": "Observation only"}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(value) or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return None
    return value


def _count(value):
    value = _number(value, minimum=0)
    return int(value) if value is not None and value.is_integer() else None


def _text(value, fallback="Unavailable"):
    return " ".join(value.split())[:160] if isinstance(value, str) and value.strip() else fallback


def _code(value, fallback="UNRECOGNIZED_CODE"):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) else fallback


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _freshness(value, now):
    parsed = _time(value)
    age = (now - parsed).total_seconds() if parsed else None
    state = ("missing_or_invalid" if parsed is None else "future_timestamp" if age < -60
             else "stale" if age > STALE_AFTER_SECONDS else "current")
    return {"timestamp": parsed.isoformat() if parsed else None, "age_seconds": age,
            "state": state, "stale_after_seconds": STALE_AFTER_SECONDS}


def _product(left, right):
    return _number(left * right) if left is not None and right is not None else None


def _progress(actual, configured, floor):
    target = _count(configured)
    target = max(floor, target) if target is not None else None
    return {"observed": actual, "required": target,
            "remaining": max(0, target - actual) if actual is not None and target is not None else None}


def _rejections(stages):
    stage = _dict(stages.get("forecast"))
    stats = _dict(stage.get("result"))
    reasons = _dict(stats.get("rejection_reasons"))
    entries, invalid = [], 0
    for index, (reason, value) in enumerate(reasons.items()):
        count = _count(value)
        if count is None:
            invalid += 1
            continue
        entries.append({"reason": _code(reason, f"REDACTED_REASON_{index + 1}"), "count": count})
    entries.sort(key=lambda row: (-row["count"], row["reason"]))
    return {
        "scope": "latest_forecast_cycle_all_venues",
        "status": "available" if stats else "not_run" if stage.get("status") == "skipped" else "unavailable",
        "skip_reason": _code(stage.get("reason")) if stage.get("status") == "skipped" else None,
        "top_reasons": entries[:8], "omitted_reason_kinds": max(0, len(entries) - 8),
        "invalid_count_entries": invalid,
        "counters": {key: _count(stats[key]) for key in COUNTERS if key in stats},
        "interpretation": "Counts may overlap and use different units; do not sum them into a rejection funnel or infer venue counts.",
    }


def _venue(venue, account, evaluation, config, *, accounting_ok, evaluation_ok, arm_incomplete):
    account, evaluation = _dict(account), _dict(evaluation)
    accounting_ok = accounting_ok and bool(account)
    evaluation_ok = evaluation_ok and bool(evaluation) and _dict(evaluation.get("criteria")).get("reads_complete") is True
    accounting = {key: (_count(account.get(source)) if is_count else _number(account.get(source), minimum=minimum))
                  if accounting_ok else None for key, source, is_count, minimum in (
                      ("seed", "initial_bankroll", False, 0), ("open", "open", True, 0),
                      ("reserved", "reserved", False, 0), ("available", "available_cash", False, 0),
                      ("equity", "equity", False, None), ("booked_pnl", "realized_pnl", False, None),
                      ("settled", "settled", True, 0), ("quarantined", "quarantined", True, 0))}
    accounting["state"] = "reported" if accounting_ok and all(v is not None for v in accounting.values()) else "incomplete"
    verified = {key: (_count(evaluation.get(key)) if is_count else _number(evaluation.get(key), minimum=minimum))
                if evaluation_ok else None for key, is_count, minimum in (
                    ("verified_settled_bets", True, 0), ("independent_traded_events", True, 0),
                    ("distinct_traded_dates", True, 0), ("verified_stake", False, 0),
                    ("verified_realized_pnl", False, None), ("quarantined", True, 0),
                    ("closing_clv_observed_bets", True, 0), ("closing_clv_complete_events", True, 0))}
    count, events = verified["verified_settled_bets"], verified["independent_traded_events"]
    pnl, stake = verified["verified_realized_pnl"], verified["verified_stake"]
    verified["net_per_verified_settled_trade"] = _number(pnl / count) if count and pnl is not None else None
    verified["net_roi"] = _number(pnl / stake) if count and stake and pnl is not None else None
    verified["mean_net_closing_clv"] = (_number(evaluation.get("mean_net_closing_clv"))
                                          if evaluation_ok and count and verified["closing_clv_complete_events"] else None)
    verified["closing_clv_event_coverage"] = (_number(evaluation.get("closing_clv_event_coverage"), minimum=0, maximum=1)
                                               if evaluation_ok and events else None)
    verified["state"] = "reported" if evaluation_ok and all(verified[key] is not None for key in (
        "verified_settled_bets", "independent_traded_events", "distinct_traded_dates", "verified_stake", "verified_realized_pnl", "quarantined")) else "incomplete"
    discrepancies = []
    for name, left, right, tolerance in (
        ("SETTLED_COUNT_DIFFERS", accounting["settled"], count, 0),
        ("BOOKED_VS_VERIFIED_PNL_DIFFERS", accounting["booked_pnl"], pnl, .0100001),
        ("OPEN_COUNT_DIFFERS", accounting["open"], _count(evaluation.get("open_bets")) if evaluation_ok else None, 0),
    ):
        if left is not None and right is not None and abs(left - right) > tolerance:
            discrepancies.append(name)
    progress = {
        "verified_traded_events": _progress(events, config.get("min_forward_events"), 250),
        "verified_traded_dates": _progress(verified["distinct_traded_dates"], config.get("min_forward_days"), 45),
    }
    criteria = _dict(evaluation.get("criteria"))
    requirements = {key: "passed" if evaluation_ok and criteria.get(key) is True else
                    "pending" if evaluation_ok and criteria.get(key) is False else "unknown" for key in CRITERIA}
    for criterion, measure in (("minimum_independent_traded_events", "verified_traded_events"),
                               ("minimum_distinct_traded_dates", "verified_traded_dates")):
        if progress[measure]["remaining"] and requirements[criterion] == "passed":
            requirements[criterion] = "pending"
            discrepancies.append("REPORTED_CRITERION_DISAGREES_WITH_COUNTS")
    incomplete = (arm_incomplete or accounting["state"] != "reported" or verified["state"] != "reported"
                  or bool(discrepancies) or bool(accounting["quarantined"]) or bool(verified["quarantined"]))
    ready = not incomplete and all(value == "passed" for value in requirements.values())
    equity, cash, reserved = accounting["equity"], accounting["available"], accounting["reserved"]
    fraction = _number(config.get("max_event_risk_fraction"), minimum=0, maximum=1)
    open_fraction = _number(config.get("max_open_risk_fraction"), minimum=0, maximum=1)
    seed = _number(config.get("seed_per_venue"), minimum=0)
    event_cap, open_cap = _product(equity, fraction), _product(equity, open_fraction)
    budget = max(0, min(cash, event_cap, open_cap - reserved)) if all(v is not None for v in (cash, event_cap, open_cap, reserved)) else None
    if accounting_ok and account.get("blocked_reasons"):
        budget = 0.0
    return {
        "venue": venue, "label": "Kalshi" if venue == "kalshi" else "Polymarket US",
        "evidence_status": "incomplete" if incomplete else "research_requirements_met" if ready else "insufficient",
        "accounting": accounting, "verified_evaluation": verified, "discrepancies": sorted(set(discrepancies)),
        "discrepancy_note": "Accounting and evaluation have separate read times and validation rules; a difference needs review, not automatic correction.",
        "progress": progress, "requirements": requirements,
        "pending_requirements": [key for key, value in requirements.items() if value != "passed"],
        "constraints": {"configured_seed_per_venue": seed, "event_risk_fraction": fraction,
                        "open_risk_fraction": open_fraction,
                        "seed_event_cost_cap": _product(seed, fraction), "current_event_cost_cap": event_cap,
                        "current_open_cost_cap": open_cap, "available_event_budget_before_market_checks": budget,
                        "minimum_net_edge_per_share": _number(config.get("min_net_edge"), minimum=0, maximum=1)},
        "economics_note": "Per-trade net is descriptive of verified settled paper trades only; zero-trade economics are undefined. Risk caps and required edge do not project profits.",
    }


def build_performance_scorecard(report: dict) -> dict:
    """Summarize existing report evidence without fetching, mutating, or pooling it."""
    report = _dict(report)
    now = datetime.now(timezone.utc)
    source = _dict(report.get("experiments"))
    if not source and _dict(report.get("experiment")).get("id"):
        source = {_text(report["experiment"]["id"]): report}
    source = dict(source)
    for identity in report.get("active_experiment_ids", []) if isinstance(report.get("active_experiment_ids"), list) else []:
        if isinstance(identity, str):
            source.setdefault(identity, {})
    experiments = {}
    for index, (identity, arm) in enumerate(source.items()):
        arm = _dict(arm)
        identity = identity if isinstance(identity, str) and identity else f"unidentified_arm_{index + 1}"
        manifest = _dict(arm.get("experiment"))
        config, stages = _dict(manifest.get("config")), _dict(arm.get("stages"))
        evaluation = _dict(arm.get("forward_evaluation"))
        if not evaluation:
            evaluation = _dict(_dict(stages.get("forward_evaluation")).get("result"))
        errors = evaluation.get("read_errors")
        read_errors = [str(item).split(":", 1)[0] if str(item).split(":", 1)[0] in READ_ERRORS else "READ_ERROR"
                       for item in errors] if isinstance(errors, list) else (["READ_ERROR"] if errors else [])
        freshness = _freshness(arm.get("completed_at"), now)
        evaluation_freshness = _freshness(evaluation.get("generated_at"), now)
        stage_issues = [{"stage": name if name in STAGES else "unrecognized_stage", "status": value.get("status")} for name, value in stages.items()
                        if isinstance(value, dict) and value.get("status") in {"failed", "degraded"}]
        identity_mismatch = (manifest.get("id") != identity
                             or evaluation.get("experiment_id") not in (None, identity))
        incomplete = (not arm or arm.get("mode") != "paper" or arm.get("status") == "failed"
                      or freshness["state"] != "current" or evaluation_freshness["state"] != "current"
                      or bool(read_errors) or identity_mismatch)
        accounting_ok = manifest.get("id") == identity and _dict(stages.get("accounting")).get("status") == "ok"
        evaluation_ok = (bool(evaluation) and not read_errors and not identity_mismatch
                         and _dict(stages.get("forward_evaluation")).get("status") not in {"failed", "degraded"})
        venues = {venue: _venue(venue, _dict(arm.get("venues")).get(venue),
                               _dict(evaluation.get("venues")).get(venue), config,
                               accounting_ok=accounting_ok, evaluation_ok=evaluation_ok, arm_incomplete=incomplete)
                  for venue in VENUES}
        experiments[identity] = {
            "label": _text(config.get("research_label"), identity),
            "source_status": arm.get("status") if arm.get("status") in {"healthy", "degraded", "failed"} else "unknown",
            "evidence_status": "incomplete" if any(v["evidence_status"] == "incomplete" for v in venues.values()) else
                               "research_requirements_met" if all(v["evidence_status"] == "research_requirements_met" for v in venues.values()) else "insufficient",
            "freshness": freshness, "evaluation_freshness": evaluation_freshness,
            "identity_mismatch": identity_mismatch, "read_error_codes": read_errors,
            "stage_issues": stage_issues, "venues": venues, "latest_cycle": _rejections(stages),
        }
    return {
        "schema_version": VERSION, "generated_at": now.isoformat(), "mode": "paper", "live_ready": False,
        "freshness": _freshness(report.get("completed_at"), now), "experiments": experiments,
        "evidence_status": "incomplete" if not experiments or any(arm["evidence_status"] == "incomplete" for arm in experiments.values()) else
                           "research_requirements_met" if all(arm["evidence_status"] == "research_requirements_met" for arm in experiments.values()) else "insufficient",
        "limitations": ["Experiments and venues have separate simulated capital; no balances or profits are combined.",
                        "Arms share markets and outcomes, so comparisons are correlated and exploratory.",
                        "Paper fills do not establish live fill quality. No income projection or live promotion follows from this scorecard."],
    }


def _escape(value):
    value = html.escape(_text(value), quote=True).replace("@", "&#64;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|\-])", r"\\\1", value)


def _display(value, *, money=False, percent=False):
    number = _number(value)
    if number is None:
        return "—"
    return f"${number:,.2f}" if money else f"{number * 100:.1f}%" if percent else f"{number:g}"


def render_scorecard_markdown(scorecard: dict) -> str:
    """Render a bounded GitHub summary, escaping untrusted labels and identities."""
    scorecard = _dict(scorecard)
    arms = _dict(scorecard.get("experiments"))
    lines = ["## Weather paper performance", "", "Separate simulated accounts. Descriptive research only; no live promotion.", "",
             f"Report freshness: {_escape(_dict(scorecard.get('freshness')).get('state'))}. Evidence: {_escape(scorecard.get('evidence_status'))}."]
    if not arms:
        lines += ["", "No completed experiment evidence is available."]
    short_requirements = dict(zip(CRITERIA, (
        "complete reads", "clean evidence", "event sample", "date sample", "ROI lower bound",
        "positive CLV", "CLV coverage", "verified decision snapshots")))
    for identity, arm in list(arms.items())[:8]:
        arm = _dict(arm)
        lines += ["", f"### {_escape(arm.get('label'))}", "", f"Experiment: {_escape(identity)} · Evidence: {_escape(arm.get('evidence_status'))}",
                  f"Completed: {_escape(_dict(arm.get('freshness')).get('timestamp'))} · Freshness: {_escape(_dict(arm.get('freshness')).get('state'))}", "",
                  "| Account | Seed | Open | Reserved | Available | Booked net | Settled | Event budget* |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for venue in VENUES:
            row = _dict(_dict(arm.get("venues")).get(venue))
            account = _dict(row.get("accounting"))
            values = [_escape(row.get("label"))] + [_display(account.get(key), money=key in {"seed", "reserved", "available", "booked_pnl"})
                        for key in ("seed", "open", "reserved", "available", "booked_pnl", "settled")]
            values.append(_display(_dict(row.get("constraints")).get("available_event_budget_before_market_checks"), money=True))
            lines.append("| " + " | ".join(values) + " |")
        constraints = _dict(_dict(_dict(arm.get("venues")).get("kalshi")).get("constraints"))
        lines += ["", f"*Before market checks: event cost ≤ {_display(constraints.get('event_risk_fraction'), percent=True)} of equity; total open cost ≤ {_display(constraints.get('open_risk_fraction'), percent=True)}. Required net edge ≥ {_display(constraints.get('minimum_net_edge_per_share'), percent=True)} per $1 payoff. These are limits, not projected returns.", "",
                  "| Verified evaluation | Trades | Net | Net/trade | Events / floor | Dates / floor | Mean closing CLV† | Event coverage |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for venue in VENUES:
            row = _dict(_dict(arm.get("venues")).get(venue))
            verified = _dict(row.get("verified_evaluation"))
            values = [_escape(row.get("label")), _display(verified.get("verified_settled_bets")),
                      _display(verified.get("verified_realized_pnl"), money=True),
                      _display(verified.get("net_per_verified_settled_trade"), money=True)]
            for key in ("verified_traded_events", "verified_traded_dates"):
                progress = _dict(_dict(row.get("progress")).get(key))
                values.append(f"{_display(progress.get('observed'))} / {_display(progress.get('required'))}")
            values.extend((_display(verified.get("mean_net_closing_clv"), money=True),
                           _display(verified.get("closing_clv_event_coverage"), percent=True)))
            lines.append("| " + " | ".join(values) + " |")
        lines += ["", "† Net CLV is dollars per share. Events/dates count verified settled trades, not forecasts or open positions."]
        for venue in VENUES:
            row = _dict(_dict(arm.get("venues")).get(venue))
            pending = row.get("pending_requirements", [])
            if isinstance(pending, list) and pending:
                lines.append(f"{_escape(row.get('label'))} — {_escape(row.get('evidence_status'))}; {len(pending)} pending/unknown: " + ", ".join(_escape(short_requirements.get(key, "unrecognized requirement")) for key in pending[:3]) + (f"; {len(pending) - 3} more in JSON." if len(pending) > 3 else "."))
            else:
                lines.append(f"{_escape(row.get('label'))}: reported research requirements met; live profitability is unproven.")
            if row.get("discrepancies"):
                lines.append(f"{_escape(row.get('label'))} accounting/evaluation difference: " + ", ".join(_escape(_code(item)) for item in row["discrepancies"][:5]) + ". Separate reads and validation rules need review.")
        cycle = _dict(arm.get("latest_cycle"))
        lines += ["", "Latest forecast cycle across both venues: " + _escape(cycle.get("status")) + ". Counts overlap; they are not a funnel or lifetime totals."]
        counters = _dict(cycle.get("counters"))
        if counters:
            shown = [(key, value) for key, value in counters.items() if value or key in {"events", "bets_placed"}]
            lines.append("Reported counters: " + "; ".join(f"{_escape(COUNTER_LABELS.get(key, key))}={_display(value)}" for key, value in shown[:6]) + ". Full counters remain in JSON.")
        reasons = cycle.get("top_reasons", [])
        if isinstance(reasons, list) and reasons:
            lines.append("Top rejection reasons: " + "; ".join(f"{_escape(REJECTION_LABELS.get(_dict(item).get('reason'), _dict(item).get('reason')))}={_display(_dict(item).get('count'))}" for item in reasons[:8]) + ". Full counts remain in JSON.")
        if arm.get("read_error_codes"):
            lines.append("Evaluation reads incomplete: " + ", ".join(_escape(_code(item)) for item in arm["read_error_codes"][:8]) + ". Missing evidence is not a zero-trade result.")
        if arm.get("stage_issues"):
            lines.append("Cycle stages needing attention: " + "; ".join(f"{_escape(_dict(item).get('stage'))}: {_escape(_dict(item).get('status'))}" for item in arm["stage_issues"][:8]) + ".")
        if arm.get("identity_mismatch"):
            lines.append("Experiment identity is missing or inconsistent; mismatched accounting/evaluation fields are omitted.")
    if len(arms) > 8:
        lines += ["", f"{len(arms) - 8} additional experiments omitted from this summary; see the JSON scorecard."]
    lines += ["", "Zero-trade economics are undefined. Small samples describe observed outcomes only; risk limits and edge thresholds do not project income. Arms share outcomes and are not independent replications.", ""]
    return "\n".join(lines)
