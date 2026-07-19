"""Regression: weather sync script must resolve `backend` when run as a file path."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]


class TestWeatherSyncInvocation(unittest.TestCase):
    def test_sync_weather_script_bootstraps_repo_root(self):
        """`python backend/models/weather/sync_weather.py` must not die on import backend."""
        script = REPO_ROOT / "backend" / "models" / "weather" / "sync_weather.py"
        # Mirror `python path/to/script.py`: path0 = script dir, no implicit cwd.
        probe = f"""
import sys, os
script = {str(script)!r}
script_dir = os.path.dirname(script)
sys.path = [script_dir] + [p for p in sys.path if p not in ("", script_dir)]
ns = {{"__name__": "__not_main__", "__file__": script}}
src = open(script, encoding="utf-8").read()
preamble = src.split("from backend.db import get_db")[0]
exec(compile(preamble + "from backend.db import get_db\\nprint('BACKEND_OK')\\n", script, "exec"), ns)
"""
        env = {**os.environ, "PAVLOV_BYPASS_CONFIG": "1"}
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertIn("BACKEND_OK", proc.stdout, msg=proc.stderr)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_ensemble_does_not_cache_empty_failures(self):
        os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
        if str(REPO_ROOT / "pavlov") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "pavlov"))
        from pipeline import ensemble_client

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = os.path.join(tmp, "ensemble_cache.json")
            bias_file = os.path.join(tmp, "ensemble_bias.json")
            with ensemble_client.isolated_storage(cache_file, bias_file):
                with mock.patch.object(ensemble_client, "_fetch_model", return_value=None):
                    result = ensemble_client._fetch_members("New York", "high")
                self.assertIsNone(result)
                # Empty failure must not be written into the TTL cache.
                cache = ensemble_client._load_cache()
                self.assertNotIn(ensemble_client._cache_key("New York", "high"), cache)

    def test_weather_markets_fetch_is_unsigned(self):
        """Weather series listing must not require Kalshi signing (CI public path)."""
        os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
        if str(REPO_ROOT / "pavlov") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "pavlov"))
        from pipeline import kalshi_client

        calls = []

        def fake_get(path, params=None, *, signed=True):
            calls.append({"path": path, "signed": signed, "params": params})
            return {"markets": []}

        with mock.patch.object(kalshi_client, "_get", side_effect=fake_get):
            with mock.patch.object(kalshi_client, "_cache_is_fresh", return_value=False):
                with mock.patch.object(kalshi_client, "_load_cache", return_value={}):
                    with mock.patch.object(kalshi_client, "_save_cache"):
                        # Only hit the first series ticker.
                        with mock.patch.object(
                            kalshi_client,
                            "_WEATHER_SERIES_TICKERS",
                            ["KXHIGHNY"],
                        ):
                            kalshi_client.get_weather_markets()
        self.assertTrue(calls)
        self.assertTrue(all(c["signed"] is False for c in calls))
        self.assertTrue(all(c["path"] == "/markets" for c in calls))

    def test_weather_import_survives_mlb_bot_pipeline_shadow(self):
        """Consensus loads pavlov-mlb-bot's pipeline before weather; import must still work."""
        os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))

        # Poison path the same way mlb_quant_legacy does during compute_all_consensus().
        mlb_bot = str(REPO_ROOT / "pavlov" / "pavlov-mlb-bot")
        sys.path.insert(0, mlb_bot)
        for name in list(sys.modules):
            if name == "pipeline" or name.startswith("pipeline."):
                del sys.modules[name]
            if name == "backend.models.weather.sync_weather":
                del sys.modules[name]
        import pipeline  # noqa: F401 — binds package to pavlov-mlb-bot

        # Fresh import of the weather sync module under the poisoned state.
        import importlib
        mod = importlib.import_module("backend.models.weather.sync_weather")
        importlib.reload(mod)

        from pipeline import settlement_resolver

        pipeline_dir = Path(settlement_resolver.__file__).resolve().parent
        self.assertEqual(
            pipeline_dir,
            (REPO_ROOT / "pavlov" / "pipeline").resolve(),
        )
        self.assertTrue(hasattr(mod, "normalize_market"))
        self.assertTrue(hasattr(mod, "sync_weather_predictions"))


if __name__ == "__main__":
    unittest.main()
