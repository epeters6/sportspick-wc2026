"""Read existing evidence and prepare a separate, prospective market guard."""
import argparse
import json
from pathlib import Path

from backend.ml.weather_learning.dataset import digest
from backend.weather_market_guard import fit, save_model
from backend.weather_model_diagnostics import diagnose
from backend.weather_probability_screen import write_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.history.read_text(encoding="utf-8-sig"))
    raw = raw["v3"] if isinstance(raw, dict) else raw
    report = diagnose(raw)
    write_once(args.output / (digest(report) + "_diagnostics.json"), report)
    guard = fit(raw)
    save_model(guard, args.output / "guard")
    print(json.dumps({"overall": report["overall"], "slices": {k:v for k,v in report["slices"].items() if k != "model_id"},
                      "cost_evidence": report["cost_evidence_rows"],
                      "guard": {k:guard[k] for k in ("model_id", "selected", "weight", "reason", "calibration_dates", "calibration_station_dates", "market_scores", "candidates")}}))


if __name__ == "__main__":
    main()
