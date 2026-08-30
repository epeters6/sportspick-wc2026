from __future__ import annotations

import unittest

from scripts.guardian_health import settlement_risk_summary


class TestGuardianSettlementIntegrity(unittest.TestCase):
    def test_invalid_negative_pnl_is_conservative_and_halts_mlb(self):
        row = {
            "id": "bad-mlb",
            "match_id": "match-1",
            "sport": "mlb",
            "status": "lost",
            "pnl": -2.0,
            "stake": 10.0,
            "shares": 20.0,
            "market_price": 0.5,
            "outcome_name": "New York Yankees",
            "bet_type": "moneyline",
            "resolved_at": "2026-07-24T02:00:00+00:00",
            "matches": {
                "id": "different-match",
                "sport": "mlb",
                "home_team": "New York Yankees",
                "away_team": "Boston Red Sox",
                "scheduled_at": "2026-07-23T23:00:00+00:00",
                "winner": "New York Yankees",
                "is_final": True,
                "home_score": 5,
                "away_score": 2,
                "match_stats": {},
            },
        }
        by_sport, total, failed = settlement_risk_summary([row])
        self.assertTrue(failed)
        self.assertEqual(by_sport["mlb"], -10.0)
        self.assertEqual(total, -10.0)

    def test_invalid_positive_pnl_becomes_full_stake_loss(self):
        row = {
            "match_id": "missing",
            "sport": "mlb",
            "pnl": 100.0,
            "stake": 7.5,
            "matches": None,
        }
        by_sport, total, failed = settlement_risk_summary([row])
        self.assertTrue(failed)
        self.assertEqual(by_sport["mlb"], -7.5)
        self.assertEqual(total, -7.5)

    def test_weather_win_without_match_is_not_invented_loss(self):
        row = {
            "match_id": None,
            "market_id": "KXWEATHER-3",
            "sport": "weather",
            "status": "won",
            "pnl": 5.0,
            "stake": 5.0,
            "shares": 10.0,
            "market_price": 0.5,
            "outcome_name": "yes",
            "metadata": {
                "settlement": {
                    "version": "weather_venue_official_v3",
                    "source": "kalshi_official",
                    "market_id": "KXWEATHER-3",
                    "official_result": "yes",
                }
            },
            "matches": None,
        }
        by_sport, total, failed = settlement_risk_summary([row])
        self.assertFalse(failed)
        self.assertEqual(by_sport["weather"], 5.0)
        self.assertEqual(total, 5.0)


if __name__ == "__main__":
    unittest.main()
