"""Unit tests for hardened live-trading toggle (auth + readiness gates)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from backend.trading import live_toggle as lt


class _FakeQuery:
    def __init__(self, table: "_FakeTable"):
        self._table = table

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return MagicMock(data=[{"value": dict(self._table.value)}])


class _FakeTable:
    def __init__(self, name: str, store: dict):
        self.name = name
        self.store = store
        self.value = store.setdefault(
            "app_settings",
            {"enabled": False, "enabled_by": None, "enabled_at": None},
        )
        self.audits = store.setdefault("audits", [])

    def select(self, *_a, **_k):
        return _FakeQuery(self)

    def eq(self, *_a, **_k):
        return _FakeQuery(self)

    def upsert(self, row, on_conflict=None):
        if self.name == "app_settings":
            self.value = dict(row.get("value") or {})
            self.store["app_settings"] = self.value
        return MagicMock(execute=lambda: MagicMock(data=[row]))

    def insert(self, row):
        if self.name == "live_toggle_audit":
            self.audits.append(dict(row))
        return MagicMock(execute=lambda: MagicMock(data=[row]))


class _FakeDB:
    def __init__(self):
        self.store: dict = {
            "app_settings": {"enabled": False},
            "audits": [],
        }

    def table(self, name: str):
        return _FakeTable(name, self.store)


class TestLiveToggleSecurity(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "LIVE_TRADING_ADMIN_TOKEN",
                "LIVE_TRADING_ADMIN_ALLOWLIST",
                "SUPABASE_JWT_SECRET",
                "GITHUB_ACTIONS",
                "ALLOW_LIVE_ON_GITHUB_ACTIONS",
            )
        }
        for k in self._env_backup:
            os.environ.pop(k, None)
        self.db = _FakeDB()

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_get_live_toggle_defaults_off(self):
        empty = MagicMock()
        empty.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        self.assertEqual(lt.get_live_toggle(empty)["enabled"], False)

        boom = MagicMock()
        boom.table.side_effect = RuntimeError("db down")
        self.assertEqual(lt.get_live_toggle(boom)["enabled"], False)

    def test_anonymous_denied(self):
        os.environ["LIVE_TRADING_ADMIN_TOKEN"] = "secret-admin"
        result = lt.request_live_toggle(
            True,
            actor="dashboard",
            authorization_header=None,
            db=self.db,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], 401)
        self.assertFalse(self.db.store["app_settings"].get("enabled"))
        self.assertTrue(self.db.store["audits"])
        self.assertFalse(self.db.store["audits"][-1]["allowed"])

    def test_non_admin_denied(self):
        os.environ["LIVE_TRADING_ADMIN_TOKEN"] = "secret-admin"
        result = lt.request_live_toggle(
            True,
            actor="dashboard",
            authorization_header="Bearer wrong-token",
            db=self.db,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], 403)
        self.assertFalse(self.db.store["app_settings"].get("enabled"))
        self.assertFalse(self.db.store["audits"][-1]["allowed"])

    @patch("backend.trading.live_toggle._guardian_halted", return_value=(False, {"halted": False}))
    @patch("backend.trading.autobet_learning.assess_live_readiness")
    def test_failed_readiness_cannot_enable(self, mock_ready, _halt):
        os.environ["LIVE_TRADING_ADMIN_TOKEN"] = "secret-admin"
        mock_ready.return_value = {
            "live_ready": False,
            "message": "need 50 more settled autobets (0/50)",
        }
        result = lt.request_live_toggle(
            True,
            actor="dashboard",
            authorization_header="Bearer secret-admin",
            db=self.db,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], 400)
        self.assertFalse(self.db.store["app_settings"].get("enabled"))
        self.assertIn("settled", result["reason"])

    @patch("backend.trading.live_toggle._guardian_halted", return_value=(False, {"halted": False}))
    @patch("backend.trading.autobet_learning.assess_live_readiness")
    def test_admin_can_enable_when_ready(self, mock_ready, _halt):
        os.environ["LIVE_TRADING_ADMIN_TOKEN"] = "secret-admin"
        mock_ready.return_value = {"live_ready": True, "message": "Ready"}
        result = lt.request_live_toggle(
            True,
            actor="dashboard",
            authorization_header="Bearer secret-admin",
            db=self.db,
        )
        self.assertTrue(result["allowed"])
        self.assertTrue(self.db.store["app_settings"].get("enabled"))
        self.assertTrue(self.db.store["audits"][-1]["allowed"])

    def test_set_live_toggle_refuses_enable(self):
        with self.assertRaises(PermissionError):
            lt.set_live_toggle(True, by="dashboard", db=self.db)

    def test_disable_allowed_for_admin(self):
        os.environ["LIVE_TRADING_ADMIN_TOKEN"] = "secret-admin"
        self.db.store["app_settings"] = {"enabled": True}
        result = lt.request_live_toggle(
            False,
            actor="dashboard",
            authorization_header="Bearer secret-admin",
            db=self.db,
        )
        self.assertTrue(result["allowed"])
        self.assertFalse(result["toggle"]["enabled"])

    @patch(
        "backend.trading.live_toggle._guardian_halted",
        return_value=(True, {"halted": True, "reasons": ["drawdown"]}),
    )
    def test_guardian_blocks_enable(self, _halt):
        os.environ["LIVE_TRADING_ADMIN_TOKEN"] = "secret-admin"
        result = lt.request_live_toggle(
            True,
            actor="ops",
            authorization_header="Bearer secret-admin",
            db=self.db,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], 409)
        self.assertFalse(self.db.store["app_settings"].get("enabled"))


if __name__ == "__main__":
    unittest.main()
