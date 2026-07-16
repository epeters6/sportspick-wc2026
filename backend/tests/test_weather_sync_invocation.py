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


if __name__ == "__main__":
    unittest.main()
