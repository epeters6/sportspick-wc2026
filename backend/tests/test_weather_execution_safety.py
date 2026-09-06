"""Regression tests for weather capital and actual-order execution accounting."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import warnings

import numpy as np

# Pavlov also runs as a standalone application and uses top-level local imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pavlov"))
from pavlov.pipeline.portfolio_optimizer import expected_log_growth, optimize_portfolio
from pavlov.polymarket import poly_client
from pavlov import data_paths as weather_data_paths
from pavlov.polymarket import poly_order_manager
from pavlov.pipeline import order_manager
from pavlov.pipeline.execution_ledger import begin_attempt


class TestWeatherBankrollSafety(unittest.TestCase):
    def test_invalid_capital_never_reaches_optimizer_or_logarithms(self):
        for bankroll in (0, -359.63, float("nan"), float("inf"), float("-inf"), None):
            with self.subTest(bankroll=bankroll), warnings.catch_warnings():
                warnings.simplefilter("error")
                with patch("pavlov.pipeline.portfolio_optimizer.minimize") as minimize:
                    with self.assertRaisesRegex(ValueError, "INVALID_BANKROLL"):
                        optimize_portfolio([0.8, 0.2], [0.4, 0.6], [100, 100], bankroll)
                    minimize.assert_not_called()

    def test_direct_objective_rejects_invalid_capital(self):
        for bankroll in (0, -1, float("nan"), float("inf")):
            with self.subTest(bankroll=bankroll), self.assertRaisesRegex(ValueError, "INVALID_BANKROLL"):
                expected_log_growth(np.zeros(2), np.array([0.5, 0.5]), np.array([0.5, 0.5]), bankroll)

    def test_empty_valid_portfolio_is_no_trade(self):
        self.assertEqual(optimize_portfolio([], [], [], 1000), [])


class TestPublicPolymarketWeather(unittest.TestCase):
    def test_weather_modules_ignore_unrelated_top_level_aliases(self):
        with patch.dict(sys.modules, {"data_paths": SimpleNamespace(), "config": SimpleNamespace(CONFIG={})}):
            importlib.reload(poly_order_manager)
            importlib.reload(order_manager)
            importlib.reload(poly_order_manager.poly_paths)
            self.assertIs(order_manager.dp, weather_data_paths)
            self.assertIs(poly_order_manager.poly_paths.dp, weather_data_paths)
            self.assertIs(poly_order_manager.poly_client, poly_client)

    def test_public_client_has_no_credentials_or_order_resource(self):
        sdk = MagicMock()
        with patch.dict(sys.modules, {"polymarket_us": SimpleNamespace(PolymarketUS=sdk)}), patch.object(poly_client, "_public_client", None):
            public = poly_client.get_public_client()
            self.assertFalse(hasattr(public, "orders"))
            self.assertFalse(hasattr(public, "account"))
            sdk.assert_called_once_with()

    def test_public_discovery_does_not_change_authenticated_client(self):
        public = MagicMock()
        public.markets.list.return_value = {"markets": []}
        public.events.list.return_value = {"events": []}
        with patch.object(poly_client, "get_public_client", return_value=public), patch.object(poly_client, "get_client") as private:
            self.assertEqual(poly_client.get_weather_markets(), [])
            private.assert_not_called()
            public.markets.list.assert_called_once()

    def test_public_book_and_settlement_do_not_request_private_credentials(self):
        public = MagicMock()
        public.markets.book.return_value = {}
        public.markets.settlement.return_value = {"settlement": 0}
        with patch.object(poly_client, "get_public_client", return_value=public), patch.object(poly_client, "get_client") as private:
            self.assertIsNone(poly_client.get_orderbook_as_parsed("weather-test"))
            self.assertEqual(poly_client.get_market_result("weather-test"), "no")
            private.assert_not_called()

    def test_private_client_still_requires_credentials_after_public_use(self):
        with patch.object(poly_client, "_client", None), patch.object(poly_client, "_public_client", MagicMock()), patch.object(poly_client, "poly_configured", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "credentials missing"):
                poly_client.get_client()


class TestPolymarketActualFills(unittest.TestCase):
    def place(self, response, side="yes", quantity=10, price=0.4):
        client = MagicMock()
        client.orders.create.return_value = response
        with patch.object(poly_client, "get_client", return_value=client):
            result = poly_client.place_order("weather-test", side, quantity, price)
        return result, client.orders.create.call_args.args[0]

    def test_accepted_zero_fill_is_not_requested_quantity(self):
        result, _ = self.place({"id": "order-1"})
        self.assertEqual(result["filled_contracts"], 0)
        self.assertEqual(result["remaining_contracts"], 10)
        self.assertTrue(result["requires_order_reconciliation"])
        self.assertIsNone(result["average_fill_price"])

    def test_new_execution_acknowledgement_is_not_a_fill(self):
        result, _ = self.place({"id": "order-1", "executions": [{"type": "EXECUTION_TYPE_NEW", "lastShares": "10", "order": {"state": "ORDER_STATE_NEW", "cumQuantity": 0}}]})
        self.assertEqual(result["filled_contracts"], 0)

    def test_partial_fractional_fill_and_duplicate_event_are_preserved(self):
        execution = {"id": "execution-1", "type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "2.5", "lastPx": {"value": "0.39"}, "order": {"id": "order-1", "cumQuantity": 2.5, "state": "ORDER_STATE_PARTIALLY_FILLED"}}
        result, _ = self.place({"id": "order-1", "executions": [execution, execution]})
        self.assertEqual(result["filled_contracts"], 2.5)
        self.assertEqual(result["remaining_contracts"], 7.5)
        self.assertAlmostEqual(result["average_fill_price"], 0.39)
        self.assertEqual(len(result["executions"]), 1)

    def test_cumulative_fill_does_not_require_all_fill_events_in_response(self):
        execution = {"type": "EXECUTION_TYPE_FILL", "lastShares": "2", "order": {"cumQuantity": 10, "state": "ORDER_STATE_FILLED", "avgPx": {"value": "0.38"}, "commissionNotionalTotalCollected": {"value": "0.05"}}}
        result, _ = self.place({"id": "order-1", "executions": [execution]})
        self.assertEqual(result["filled_contracts"], 10)
        self.assertEqual(result["remaining_contracts"], 0)
        self.assertEqual(result["average_fill_price"], 0.38)
        self.assertEqual(result["fees_paid"], 0.05)

    def test_no_limit_and_execution_prices_use_the_correct_basis(self):
        execution = {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "1.25", "lastPx": {"value": "0.60"}}
        result, body = self.place({"id": "order-1", "executions": [execution]}, side="no", quantity=10.5)
        self.assertEqual(body["price"]["value"], "0.59")
        self.assertEqual(body["quantity"], 10.5)
        self.assertEqual(result["price"], 0.41)
        self.assertAlmostEqual(result["average_fill_price"], 0.40)
        self.assertEqual(result["filled_contracts"], 1.25)

    def test_malformed_fill_cannot_fabricate_a_position(self):
        result, _ = self.place({"id": "order-1", "executions": [{"type": "EXECUTION_TYPE_FILL", "lastShares": "nan"}]})
        self.assertEqual(result["filled_contracts"], 0)
        self.assertTrue(result["requires_order_reconciliation"])

    def test_filled_state_without_actual_fill_quantity_requires_reconciliation(self):
        result, _ = self.place({"id": "order-1", "executions": [{"type": "EXECUTION_TYPE_FILL", "order": {"state": "ORDER_STATE_FILLED"}}]})
        self.assertEqual(result["filled_contracts"], 0)
        self.assertTrue(result["requires_order_reconciliation"])

    def test_invalid_order_is_rejected_before_client_or_network(self):
        with patch.object(poly_client, "get_client") as get_client:
            result = poly_client.place_order("weather-test", "yes", float("nan"), 0.4)
            self.assertEqual(result["status"], "error")
            get_client.assert_not_called()


class TestWeatherPendingOrders(unittest.IsolatedAsyncioTestCase):
    SIGNAL = {"ticker": "weather-test", "recommended_side": "yes", "kelly_contracts": 10, "implied_prob": 0.4}

    async def test_poly_zero_partial_and_full_fills_are_held_until_reconciled(self):
        for fills in (0, 2.5, 10):
            with self.subTest(fills=fills), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "positions.json")
                response = {"order_id": "order-1", "status": "ok", "filled_contracts": fills, "price": 0.41}
                with patch.object(poly_order_manager.poly_paths, "POSITIONS", path), patch.dict(poly_order_manager.CONFIG, {"POLY_MIN_NOTIONAL_USD": 0}), patch.object(poly_order_manager.poly_client, "place_order", return_value=response) as send:
                    first = await poly_order_manager.place_trade(self.SIGNAL)
                    second = await poly_order_manager.place_trade(self.SIGNAL)
                    row = json.loads(Path(path).read_text())[0]
                self.assertTrue(first["pending"])
                self.assertFalse(first["success"])
                self.assertIn("ORDER_RECONCILIATION_REQUIRED", second["error"])
                self.assertEqual(row["status"], "pending_order")
                self.assertEqual(row["kelly_contracts"], fills)
                self.assertEqual(row["requested_contracts"], 10)
                self.assertAlmostEqual(row["reserved_notional"], 4.1)
                self.assertIsNone(row["pl"])
                send.assert_called_once()

    async def test_kalshi_partial_fill_is_not_requested_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "positions.json")
            client = MagicMock()
            client.place_order.return_value = {"order_id": "kalshi-1", "status": "resting", "filled_contracts": 3}
            with patch.object(order_manager, "_POSITIONS_FILE", path):
                await order_manager.place_trade(self.SIGNAL, client)
                await order_manager.place_trade(self.SIGNAL, client)
            row = json.loads(Path(path).read_text())[0]
            self.assertEqual(row["kelly_contracts"], 3)
            self.assertEqual(row["status"], "pending_order")
            client.place_order.assert_called_once()

    async def test_unknown_submission_blocks_retry_even_without_order_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "positions.json")
            client = MagicMock()
            client.place_order.side_effect = TimeoutError("response lost")
            with patch.object(order_manager, "_POSITIONS_FILE", path):
                result = await order_manager.place_trade(self.SIGNAL, client)
                await order_manager.place_trade(self.SIGNAL, client)
            self.assertTrue(result["pending"])
            self.assertEqual(json.loads(Path(path).read_text())[0]["order_status"], "submission_unknown")
            client.place_order.assert_called_once()

    async def test_corrupt_ledger_never_submits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "positions.json")
            Path(path).write_text("broken json")
            client = MagicMock()
            with patch.object(order_manager, "_POSITIONS_FILE", path):
                result = await order_manager.place_trade(self.SIGNAL, client)
            self.assertFalse(result["success"])
            client.place_order.assert_not_called()

    async def test_concurrent_submissions_reserve_only_one_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "positions.json")
            client = MagicMock()
            client.place_order.return_value = {"order_id": "kalshi-1", "status": "resting", "filled_contracts": 0}
            with patch.object(order_manager, "_POSITIONS_FILE", path):
                await asyncio.gather(*(order_manager.place_trade(self.SIGNAL, client) for _ in range(2)))
            client.place_order.assert_called_once()
            self.assertEqual(len(json.loads(Path(path).read_text())), 1)

    def test_existing_submission_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "positions.json")
            Path(f"{path}.submission.lock").touch()
            with self.assertRaisesRegex(ValueError, "ORDER_RECONCILIATION_REQUIRED"):
                begin_attempt(path, self.SIGNAL, venue="kalshi", quantity=10, price=0.4)


if __name__ == "__main__":
    unittest.main()
