"""Weather mode cannot inherit live clearance or expose mutation endpoints anonymously."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.api import main as api


class TestWeatherAPIFocus(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.settings = SimpleNamespace(trading_focus="weather", polymarket_live_min_settled_bets=50,
                                        polymarket_live_min_roi_pct=0)

    def test_mutations_and_balances_require_authorization(self):
        with patch.object(api, "get_db", side_effect=AssertionError("must not query account")):
            for path in ("/sync", "/seed", "/trading/autobet/run"):
                self.assertIn(self.client.post(path).status_code, (401, 403))
            self.assertIn(self.client.get("/trading/treasury").status_code, (401, 403))

    def test_database_failure_cannot_look_like_a_fresh_empty_experiment(self):
        with patch.object(api, "get_db", side_effect=RuntimeError("unavailable")):
            response = self.client.get("/weather/experiment")
        self.assertEqual(response.status_code, 503)

    def test_model_research_evidence_never_grants_live_clearance(self):
        evidence = {"forward_evaluation": {"research_evidence_ready": True,
                    "venues": {"kalshi": {"criteria": {"positive_net_closing_clv": True}}}}}
        with patch("backend.config.get_settings", return_value=self.settings), \
                patch.object(api, "weather_experiment_status", return_value=evidence):
            response = self.client.get("/models/readiness")
        self.assertEqual(response.status_code, 200)
        weather = response.json()["weather_portfolio"]
        self.assertTrue(weather["research_evidence_ready"])
        self.assertFalse(weather["ready"])

    def test_profitable_legacy_results_do_not_clear_weather(self):
        with patch("backend.config.get_settings", return_value=self.settings), \
                patch("backend.trading.autobet_learning.assess_live_readiness", return_value={"live_ready": True}), \
                patch("backend.trading.autobet_learning.compute_sport_stats", return_value={"weather": {"settled": 1000, "roi_pct": 20}}), \
                patch("backend.trading.live_toggle.get_live_toggle", return_value={"enabled": True}), \
                patch("backend.trading.live_toggle.is_live_mode", return_value=False):
            response = self.client.get("/trading/readiness")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["can_enable_live"])
        self.assertFalse(response.json()["domains"][0]["is_ready"])
