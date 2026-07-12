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
    report = run_analysis()
    
    # 4. Write timestamped report
    today_str = datetime.now().strftime("%Y-%m-%d")
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
