"""Regression: old tuned params cannot silently override an unrobust live strategy."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import main


class TunedParametersTests(unittest.TestCase):
    def test_tuned_parameters_require_robust_oos_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            cfg = {
                "mode": "paper", "profile": "assertief",
                "profiles": {"assertief": {}},
                "agents": {"trend4h": {"lookback": 55, "timeframe_minutes": 240}},
                "backtest": {"use_tuned_params": True, "min_trades": 30},
                "universe": {"size": 8},
            }
            (root / "config.json").write_text(json.dumps(cfg))
            (root / "data" / "tuned_params.json").write_text(json.dumps({
                "trend4h": {"lookback": 80}, "_universe_size": 15,
            }))
            report = {
                "agents": {"trend4h": {"tuned": True, "timeframe_minutes": 240,
                    "oos_trades": 52, "oos_expectancy_r": 0.193,
                    "oos_ex_best_expectancy_r": -0.067, "params": {"lookback": 80},
                }}
            }
            (root / "data" / "backtest.json").write_text(json.dumps(report))
            with patch.object(main, "ROOT", root), patch.object(
                main, "BACKTEST_PATH", root / "data" / "backtest.json"
            ):
                loaded = main.load_config()
                self.assertEqual(loaded["agents"]["trend4h"]["lookback"], 55)
                self.assertEqual(loaded["universe"]["size"], 8)
                report["agents"]["trend4h"]["oos_ex_best_expectancy_r"] = 0.12
                (root / "data" / "backtest.json").write_text(json.dumps(report))
                loaded = main.load_config()
                self.assertEqual(loaded["agents"]["trend4h"]["lookback"], 80)


if __name__ == "__main__":
    unittest.main()
