"""Run the frozen paper suite and publish separate, read-only research diagnostics.

The observer runs after all approaches finish. It does not fetch market data,
change a decision, write database records, or replace the original cycle report.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def suite_exit_code(report: dict) -> int:
    """Keep the existing cycle command's failure contract."""
    return int(report.get("status") == "failed" or any(
        stage.get("status") == "failed" for stage in report.get("stages", {}).values()
    ))


def render_discovery_markdown(discovery: dict) -> str:
    def safe(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")

    lines = ["", "### Captured temperature ranges", ""]
    status = discovery.get("status")
    if status == "not_captured":
        return "\n".join(lines + [
            "No discovery capture in this run; this is expected when forecasting is skipped. "
            "It does not mean there are zero opportunities.", "",
        ])
    if status not in {"ok", "issues_found"}:
        lines += ["Discovery diagnostics are incomplete; use the original cycle report.", ""]
    lines += [
        f"Capture time: {safe(discovery.get('captured_at') or 'unavailable')}. "
        f"Scope checked at: {safe(discovery.get('eligibility_as_of') or 'unavailable')}.", "",
        "| Venue | Event groups | Complete ranges | Incomplete ranges |",
        "| --- | ---: | ---: | ---: |",
    ]
    for venue, summary in discovery.get("venues", {}).items():
        lines.append(f"| {safe(venue)} | {safe(summary.get('groups'))} | "
                     f"{safe(summary.get('complete_partitions'))} | {safe(summary.get('invalid_partitions'))} |")
    invalid = [group for group in discovery.get("groups", []) if not group.get("partition", {}).get("complete")]
    if invalid:
        lines += ["", "Incomplete groups kept out of trading:", ""]
        for group in invalid:
            reasons = sorted({issue.get("reason", "unknown") for issue in group["partition"].get("issues", [])})
            lines.append(f"- {safe(group.get('venue'))}, {safe(group.get('station'))}, "
                         f"{safe(group.get('target_date'))}, {safe(group.get('metric'))}: {safe(', '.join(reasons))}.")
    lines += ["", "Exact public contract IDs, range boundaries, and scope by approach are in "
              "`opportunities.json` in this run's evidence artifact. These are diagnostic counts, "
              "not a count of executable trades.", ""]
    return "\n".join(lines)


async def run_observed_suite(*, report_path="reports/weather/latest.json", db=None,
                             configs=None, now=None, suite_runner=None,
                             cycle_runner=None) -> tuple[dict, dict]:
    from backend.trading.weather_research import read_research_configs, run_research_suite
    from scripts.run_weather_cycle import run_cycle

    configs = read_research_configs() if configs is None else configs
    started = now
    runner = cycle_runner or run_cycle
    shared = {}

    async def observe_cycle(**kwargs):
        nonlocal shared, started
        # Retain the existing capture by reference; don't prefetch, copy into
        # the engine, or add observer state to its shared decision inputs.
        shared = kwargs["shared_inputs"]
        started = kwargs["now"]
        return await runner(**kwargs)

    report = await (suite_runner or run_research_suite)(
        report_path=report_path, db=db, configs=configs, now=now,
        cycle_runner=observe_cycle,
    )
    evidence = {
        "schema_version": 1, "mode": "paper", "live_ready": False,
        "status": "ok", "suite_run_id": report.get("run_id"),
        "cycle_timestamp": report.get("timestamp"),
        "cycle_completed_at": report.get("completed_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "components": {},
    }
    markdown = "Weather paper research diagnostics could not be generated. See the cycle report.\n"
    try:
        from backend.trading.weather_opportunity_diagnostics import build_discovery_diagnostics
        if started is None:
            started = datetime.fromisoformat(report["timestamp"].replace("Z", "+00:00"))
        evidence["discovery"] = build_discovery_diagnostics(shared, configs, now=started)
        diagnostic_status = evidence["discovery"].get("status")
        discovery_ok = diagnostic_status in {"ok", "issues_found", "not_captured"}
        evidence["components"]["discovery"] = {"status": "ok" if discovery_ok else "failed"}
        if not discovery_ok:
            evidence["status"] = "degraded"
    except Exception as exc:
        evidence["status"] = "degraded"
        evidence["components"]["discovery"] = {"status": "failed", "error": type(exc).__name__}
    try:
        from backend.trading.weather_performance_scorecard import (
            build_performance_scorecard, render_scorecard_markdown,
        )
        evidence["performance"] = build_performance_scorecard(report)
        markdown = render_scorecard_markdown(evidence["performance"])
        evidence["components"]["performance"] = {"status": "ok"}
    except Exception as exc:
        evidence["status"] = "degraded"
        evidence["components"]["performance"] = {"status": "failed", "error": type(exc).__name__}

    if "discovery" in evidence:
        markdown += render_discovery_markdown(evidence["discovery"])
    else:
        markdown += "\nDiscovery diagnostics failed; the original cycle report remains available.\n"

    output = Path(report_path).parent
    # These artifacts are supplementary. Never overwrite the authoritative
    # latest.json, individual approach reports, or the durable database report.
    _write_json(output / "opportunities.json", evidence)
    (output / "performance.md").write_text(markdown, encoding="utf-8")
    return report, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="reports/weather/latest.json")
    args = parser.parse_args()
    os.environ.update({
        "LIVE_TRADING_ENABLED": "false", "POLYMARKET_LIVE_ENABLED": "false",
        "AUTO_BET_ENABLED": "0", "POLY_AUTO_BET_ENABLED": "0",
        "PAVLOV_BYPASS_CONFIG": "1", "DISCORD_WEBHOOK_URL": "",
    })
    report, evidence = asyncio.run(run_observed_suite(report_path=args.report))
    print(json.dumps({
        "status": report["status"], "mode": "paper", "run_id": report["run_id"],
        "stages": {key: value["status"] for key, value in report["stages"].items()},
        "diagnostics_status": evidence["status"],
    }))
    return max(suite_exit_code(report), int(evidence["status"] != "ok"))


if __name__ == "__main__":
    raise SystemExit(main())
