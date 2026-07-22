"""Regression: setup_daily_slate must not UnboundLocalError on datetime."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
ORCH_PATH = REPO_ROOT / "backend" / "ml" / "mlb_quant" / "orchestrator.py"


class TestMlbOrchestratorSetup(unittest.TestCase):
    def test_setup_daily_slate_has_no_local_datetime_import(self):
        source = ORCH_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        setup_fn = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "setup_daily_slate":
                setup_fn = node
                break
        self.assertIsNotNone(setup_fn, "setup_daily_slate not found")

        local_datetime_imports = []
        for node in ast.walk(setup_fn):
            if isinstance(node, ast.ImportFrom) and node.module == "datetime":
                names = [a.name for a in node.names]
                if "datetime" in names:
                    local_datetime_imports.append(node.lineno)
        self.assertEqual(
            local_datetime_imports,
            [],
            f"local `from datetime import datetime` inside setup_daily_slate "
            f"causes UnboundLocalError (lines {local_datetime_imports})",
        )

    def test_setup_daily_slate_callable_without_datetime_unbound(self):
        """Call setup_daily_slate with heavy deps mocked; datetime must resolve."""
        import backend.ml.mlb_quant.orchestrator as orch

        # Empty slate path — still exercises datetime usage in try/except DB upsert.
        with mock.patch.object(orch, "load_existing_manifest", return_value={}):
            with mock.patch.object(orch, "fetch_today_starters", return_value=[]):
                with mock.patch.object(orch, "fetch_today_matchups", return_value={}):
                    with mock.patch.object(orch, "fetch_team_offense_context", return_value={}):
                        with mock.patch.object(orch, "fetch_recent_bullpen_data", return_value=[]):
                            with mock.patch.object(
                                orch, "build_bullpen_context_by_team", return_value={}
                            ):
                                with mock.patch.object(orch, "load_umpire_overrides", return_value={}):
                                    with mock.patch.object(
                                        orch, "load_tier_overrides", return_value=({}, {})
                                    ):
                                        with mock.patch.object(
                                            orch,
                                            "_pyb",
                                            return_value=mock.Mock(
                                                pitching_stats_range=mock.Mock(
                                                    side_effect=RuntimeError("skip")
                                                )
                                            ),
                                        ):
                                            with mock.patch(
                                                "backend.db.get_db",
                                                side_effect=RuntimeError("no db"),
                                            ):
                                                with mock.patch.object(orch, "atomic_write_json"):
                                                    with mock.patch(
                                                        "backend.ml.mlb_quant.fetch_props.update_manifest_with_props",
                                                        side_effect=RuntimeError("skip props"),
                                                    ):
                                                        orch.setup_daily_slate()


if __name__ == "__main__":
    unittest.main()
