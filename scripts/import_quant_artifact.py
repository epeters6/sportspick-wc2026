import sys
import os
import shutil
import zipfile
import json
from datetime import datetime

TARGET_FILES = [
    "sports_shadow_decisions.jsonl",
    "sports_paper_fills.jsonl",
    "sports_clv_tracking.jsonl",
    "weather_shadow_decisions.jsonl",
    "paper_fills.jsonl",
    "clv_tracking.jsonl",
    "orderbook_snapshots.jsonl",
    "sync_status.json"
]

def main(source_path):
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # 1. Prepare backup directory
    backup_timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_dir = os.path.join(root_dir, "validation_backups", backup_timestamp)
    reports_backup_dir = os.path.join(backup_dir, "reports", "sports_shadow")
    
    # 2. Extract or locate source files
    temp_extract_dir = None
    if os.path.isfile(source_path) and source_path.endswith('.zip'):
        temp_extract_dir = os.path.join(root_dir, "validation_backups", "temp_extract")
        os.makedirs(temp_extract_dir, exist_ok=True)
        with zipfile.ZipFile(source_path, 'r') as zip_ref:
            zip_ref.extractall(temp_extract_dir)
        source_dir = temp_extract_dir
    elif os.path.isdir(source_path):
        source_dir = source_path
    else:
        print(f"Error: {source_path} is not a valid zip file or directory.")
        sys.exit(1)
        
    # 3. Perform Backups
    os.makedirs(backup_dir, exist_ok=True)
    os.makedirs(reports_backup_dir, exist_ok=True)
    
    for fname in TARGET_FILES:
        local_path = os.path.join(root_dir, fname)
        if os.path.exists(local_path):
            shutil.copy2(local_path, os.path.join(backup_dir, fname))
            
    local_reports_dir = os.path.join(root_dir, "reports", "sports_shadow")
    if os.path.exists(local_reports_dir):
        for f in os.listdir(local_reports_dir):
            if f.endswith('.json'):
                shutil.copy2(os.path.join(local_reports_dir, f), os.path.join(reports_backup_dir, f))
                
    # 4. Copy new files
    files_imported = 0
    rows_imported = {}
    
    for fname in TARGET_FILES:
        src = os.path.join(source_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(root_dir, fname))
            files_imported += 1
            if fname.endswith('.jsonl'):
                with open(src, 'r') as f:
                    rows_imported[fname] = sum(1 for line in f if line.strip())
                    
    src_reports = os.path.join(source_dir, "reports", "sports_shadow")
    
    if os.path.exists(src_reports):
        os.makedirs(local_reports_dir, exist_ok=True)
        for f in os.listdir(src_reports):
            if f.endswith('.json'):
                shutil.copy2(os.path.join(src_reports, f), os.path.join(local_reports_dir, f))
                files_imported += 1
                
    if temp_extract_dir and os.path.exists(temp_extract_dir):
        shutil.rmtree(temp_extract_dir)
        
    # 5. Print Summary
    print(f"=== Artifact Import Complete ===")
    print(f"Files imported: {files_imported}")
    print("Rows imported per JSONL:")
    for k, v in rows_imported.items():
        print(f"  {k}: {v}")
        
    sync_status_path = os.path.join(root_dir, "sync_status.json")
    if os.path.exists(sync_status_path):
        try:
            with open(sync_status_path, 'r') as f:
                status = json.load(f)
            print(f"\nSync Status Summary:")
            print(f"  Last Status: {status.get('last_status')}")
            print(f"  Mode: {status.get('mode')}")
            print(f"  Last Run: {status.get('last_started_at')}")
        except:
            pass
            
    if os.path.exists(local_reports_dir):
        reports = [f for f in os.listdir(local_reports_dir) if f.endswith('_report.json')]
        if reports:
            latest = sorted(reports)[-1]
            print(f"\nLatest Report: {latest}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_quant_artifact.py <path-to-zip-or-folder>")
        sys.exit(1)
    main(sys.argv[1])
