import asyncio
import os
import sys
import json
from datetime import datetime
from loguru import logger

# Add root directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.models.sports.run_shadow_mlb import run_mlb_shadow_execution
from scripts.run_clv_scheduler import run_scheduler
from scripts.analyze_sports_shadow import run_analysis

async def run_validation():
    logger.info("Running Daily Sports Shadow Validation...")
    start_time = datetime.now()
    status_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_status.json")

    # Fresh decision/fill artifacts each run. Do NOT truncate CLV history —
    # checkpoints must persist across GitHub runners.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("sports_shadow_decisions.jsonl", "sports_paper_fills.jsonl"):
        path = os.path.join(repo_root, name)
        try:
            open(path, "w").close()
        except OSError as exc:
            logger.warning(f"Could not reset {name}: {exc}")
    
    def write_status(exit_code=0, error_msg=None, completed=False):
        duration = (datetime.now() - start_time).total_seconds()
        status_data = {
            "last_started_at": start_time.isoformat(),
            "last_finished_at": datetime.now().isoformat() if completed or error_msg else None,
            "last_duration_seconds": duration,
            "last_exit_code": exit_code,
            "last_status": "success" if exit_code == 0 and not error_msg else "failed" if error_msg else "running",
            "last_error": error_msg,
            "mode": "shadow",
            "mlb_shadow_started": True,
            "mlb_shadow_completed": completed,
            "clv_scheduler_once_completed": completed,
            "report_written": None
        }
        try:
            with open(status_file, "w") as f:
                json.dump(status_data, f, indent=2)
        except:
            pass
            
    write_status()
    
    try:
        # Ensure no live orders can be submitted by environment variables or hacks.
        os.environ["MODE"] = "shadow"

        # 1. Run MLB shadow mode
        logger.info("Phase 1: Running MLB Shadow Mode...")
        await run_mlb_shadow_execution()

        # 2. Run CLV scheduler once
        logger.info("Phase 2: Running CLV Scheduler (once)...")
        await run_scheduler(once=True)

        # 3. Run analysis
        logger.info("Phase 3: Running Shadow Analysis...")
        decisions_path = os.path.join(repo_root, "sports_shadow_decisions.jsonl")
        if not os.path.exists(decisions_path):
            raise FileNotFoundError(
                f"Missing sports shadow decisions manifest: {decisions_path}"
            )
        report = run_analysis(decisions_file=decisions_path)

        # Attach moneyline per-venue + pitcher-outs availability from status artifact
        today_str = datetime.now().strftime("%Y-%m-%d")
        status_art = os.path.join(
            repo_root, "reports", "sports_shadow", f"{today_str}_mlb_shadow_status.json"
        )
        if os.path.exists(status_art):
            try:
                with open(status_art, "r") as f:
                    mlb_status = json.load(f)
                report["mlb_moneyline"] = mlb_status.get("moneyline")
                report["pitcher_outs"] = mlb_status.get("pitcher_outs")
                report["by_venue"] = (mlb_status.get("moneyline") or {}).get("by_venue")
            except Exception as exc:
                logger.warning(f"Could not merge mlb shadow status: {exc}")

        # 4. Write timestamped report
        report_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "sports_shadow")
        os.makedirs(report_dir, exist_ok=True)

        report_path = os.path.join(report_dir, f"{today_str}_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
            
        # Update final status
        write_status(completed=True)
        try:
            with open(status_file, "r") as f:
                data = json.load(f)
            data["report_written"] = f"reports/sports_shadow/{today_str}_report.json"
            with open(status_file, "w") as f:
                json.dump(data, f, indent=2)
        except:
            pass
            
        logger.info(f"Validation complete. Report written to {report_path}")
    except Exception as e:
        write_status(exit_code=1, error_msg=str(e), completed=False)
        raise
    
if __name__ == "__main__":
    asyncio.run(run_validation())
