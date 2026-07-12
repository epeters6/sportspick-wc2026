import unittest
import os
import json
import sys

# Ensure scripts directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.analyze_sports_shadow import run_analysis

class TestAnalyzeSportsShadow(unittest.TestCase):
    def setUp(self):
        self.decisions_file = "test_decisions.jsonl"
        self.fills_file = "test_fills.jsonl"
        self.clv_file = "test_clv.jsonl"
        
        with open(self.decisions_file, "w") as f:
            f.write(json.dumps({
                "rejection_reason": None,
                "P_model": 0.55,
                "market_type": "moneyline",
                "calibration_status": "uncalibrated_shadow"
            }) + "\n")
            f.write(json.dumps({
                "rejection_reason": "EDGE_GONE",
                "P_model": 0.45,
                "market_type": "prop",
                "calibration_status": "calibrated_out_of_sample"
            }) + "\n")
            
        with open(self.fills_file, "w") as f:
            f.write(json.dumps({"filled_shares": 10}) + "\n")
            
    def tearDown(self):
        if os.path.exists(self.decisions_file):
            os.remove(self.decisions_file)
        if os.path.exists(self.fills_file):
            os.remove(self.fills_file)
        if os.path.exists(self.clv_file):
            os.remove(self.clv_file)

    def test_analyze_sports_shadow_reads_jsonl(self):
        report = run_analysis(self.decisions_file, self.fills_file, self.clv_file)
        self.assertEqual(report["total_predictions"], 2)

    def test_analyze_sports_shadow_counts_rejections(self):
        report = run_analysis(self.decisions_file, self.fills_file, self.clv_file)
        self.assertEqual(report["total_rejections"], 1)
        self.assertEqual(report["rejection_reason_counts"]["EDGE_GONE"], 1)

    def test_analyze_sports_shadow_counts_would_trade(self):
        report = run_analysis(self.decisions_file, self.fills_file, self.clv_file)
        self.assertEqual(report["total_would_trade"], 1)

    def test_analyze_sports_shadow_groups_by_market_type(self):
        report = run_analysis(self.decisions_file, self.fills_file, self.clv_file)
        self.assertEqual(report["groups"]["by_market_type"]["moneyline"], 1)
        self.assertEqual(report["groups"]["by_market_type"]["prop"], 1)

    def test_analyze_sports_shadow_handles_missing_clv(self):
        # self.clv_file does not exist here because it's not created in setUp
        report = run_analysis(self.decisions_file, self.fills_file, self.clv_file)
        self.assertEqual(report["average_clv_15m"], 0)

if __name__ == "__main__":
    unittest.main()
