"""Regression checks for Kraken engine guardrails (no API keys and no live orders)."""
import unittest
from types import SimpleNamespace

from agents.execution import LiveExecutionAgent, advance
from agents.learning import LearningAgent
from agents.risk import RiskAgent


class EngineGuardrailTests(unittest.TestCase):
    def test_trailing_stop_never_moves_backward(self):
        pos = {
            "exit_style": "two_stage", "last_checked_ts": 100, "last_4h_ts": 0,
            "stop": 105.0, "hard_stop": 103.0, "peak": 120.0,
            "entry_price": 100.0, "is_runner": False, "initial_atr": 2.0,
            "runner_atr": 6.0, "runner_threshold_atr": 1.0,
            "candles_held": 0,
        }
        out = advance(
            pos, [], [{"ts": 14400, "close": 120.0, "atr": 10.0}],
            {"catastrophe_atr": 1.0}, 0.004, 0.001, 3600, False,
        )
        self.assertIsNone(out)
        self.assertEqual(pos["stop"], 105.0)
        self.assertEqual(pos["hard_stop"], 103.0)
        self.assertTrue(pos["is_runner"])

    def test_weak_out_of_sample_robustness_does_not_promote(self):
        backtest = {
            "trend4h": {
                "timeframe_minutes": 240, "oos_trades": 52,
                "oos_expectancy_r": 0.193, "oos_ex_best_expectancy_r": -0.067,
                "expectancy_r": 0.286, "ex_best_expectancy_r": 0.20,
            },
        }
        learner = LearningAgent(
            {}, {"enabled": True, "min_trades": 5, "window": 20, "min_weight": 0.3, "max_weight": 1.6},
            {"enabled": True, "all_agents_live": False, "backtest_min_r": 0.1},
            {"trend4h": {"start": "schaduw", "timeframe_minutes": 240}},
            backtest, 30,
        )
        self.assertEqual(learner.bt_verdict("trend4h"), "neutraal")
        backtest["trend4h"]["oos_ex_best_expectancy_r"] = 0.15
        self.assertEqual(learner.bt_verdict("trend4h"), "positief")
        backtest["trend4h"].pop("oos_expectancy_r")
        self.assertEqual(learner.bt_verdict("trend4h"), "geen")

    def test_unverified_stop_blocks_further_live_entries(self):
        class Exchange:
            def market(self, market):
                return {"base": "BTC", "limits": {"amount": {"min": 0}, "cost": {"min": 0}}}

            def amount_to_precision(self, market, amount):
                return str(amount)

            def create_order(self, *args, **kwargs):
                return {"id": "test-only-buy"}

        book = {
            "name": "live", "cash": 200.0, "fees_paid": 0.0,
            "positions": {}, "risk_status": {"trading_enabled": True, "halted": False, "daily_limit_hit": False},
        }
        errs = []
        cfg = {
            "fee_pct": 0.4, "slippage_pct": 0.08,
            "exits": {"breakeven_at_r": 1.0, "trail_after_r": 1.5},
        }
        x = LiveExecutionAgent(Exchange(), cfg, 15, 3, False, errs)
        x._wait_fill = lambda _id, _market: (1.0, 100.0, 100.0, 0.4, 0.0, {})
        x._place_stop = lambda _pos: (_ for _ in ()).throw(RuntimeError("simulated rejection"))
        signal = SimpleNamespace(exit_style="r_trail", trailing=True, max_candles=0)
        result = x.buy(
            book, "BTC/EUR", "momentum", signal, 1.0, 100.0, 90.0, 90.0,
            {}, 10.0, 1000, "test", 0.7, {},
        )
        self.assertTrue(result["ok"])  # the actual buy has already happened
        self.assertTrue(book["positions"]["BTC/EUR"]["protection_pending"])
        self.assertFalse(book["risk_status"]["trading_enabled"])
        self.assertIn("stop-loss", book["trading_blocked_reason"])
        self.assertTrue(any("KRITIEK" in msg for msg in errs))
        approved = RiskAgent(
            {"max_open_positions": 4, "max_daily_loss_pct": 5,
             "risk_per_trade_pct": 1, "max_position_pct": 100, "max_exposure_pct": 100}, 5,
        ).approve_entry(
            book, book["risk_status"], 100.0, 0.0, "ETH/EUR", 100.0, 90.0,
            100.0, 0.7, 1000,
        )
        self.assertFalse(approved[0])

    def test_unconfirmed_buy_fill_is_not_invented(self):
        class Exchange:
            def market(self, market):
                return {"base": "BTC", "limits": {"amount": {"min": 0}, "cost": {"min": 0}}}

            def amount_to_precision(self, market, amount):
                return str(amount)

            def create_order(self, *args, **kwargs):
                return {"id": "uncertain-buy-123"}

        errors = []
        x = LiveExecutionAgent(
            Exchange(), {"fee_pct": 0.4, "slippage_pct": 0.08, "exits": {}},
            15, 3, False, errors,
        )
        x._wait_fill = lambda *_: (_ for _ in ()).throw(RuntimeError("timeout"))
        book = {
            "name": "live", "cash": 100.0, "fees_paid": 0.0,
            "positions": {}, "risk_status": {"trading_enabled": True},
        }
        sig = SimpleNamespace(exit_style="r_trail", trailing=True, max_candles=0)
        result = x.buy(book, "BTC/EUR", "momentum", sig, 0.1, 100.0,
                       90.0, 90.0, {}, 1.0, 1000, "test", 0.8, {})
        self.assertFalse(result["ok"])
        self.assertEqual(book["cash"], 100.0)  # no made-up execution values
        self.assertEqual(book["positions"], {})
        self.assertEqual(book["unresolved_order"]["id"], "uncertain-buy-123")
        self.assertFalse(book["risk_status"]["trading_enabled"])
        self.assertTrue(any("KRITIEK" in e for e in errors))


    def test_partial_exchange_stop_is_not_logged_as_full_sale(self):
        class Exchange:
            def price_to_precision(self, market, amount):
                return str(amount)
        book = {"name": "live", "positions": {"BTC/EUR": {}},
                "risk_status": {"trading_enabled": True}}
        errors = []
        x = LiveExecutionAgent(
            Exchange(), {"fee_pct": 0.4, "slippage_pct": 0.08, "exits": {}},
            15, 3, False, errors,
        )
        x.now = 12345
        pos = {"market": "BTC/EUR", "qty": 1.0, "entry_price": 100.0,
               "exchange_stop_price": 90.0}
        order = {"id": "partial-stop", "status": "closed",
                 "filled": 0.2, "average": 90., "cost": 18.0}
        with self.assertRaisesRegex(RuntimeError, "gedeeltelijk"):
            x._exit_from_order(book, pos, order, "partial", None)
        self.assertFalse(book["risk_status"]["trading_enabled"])
        self.assertIn("BTC/EUR", book["positions"])
        self.assertEqual(book["unresolved_order"]["id"], "partial-stop")

    def test_unknown_stop_does_not_invent_a_trade(self):
        class Exchange:
            def market(self, market):
                return {"base": "BTC"}
            def fetch_order(self, oid, market):
                raise TimeoutError("exchange did not respond")
            def fetch_balance(self):
                return {"total": {"BTC": 0.0}}

        cfg = {"fee_pct": .4, "slippage_pct": .08, "exits": {}}
        errors = []
        x = LiveExecutionAgent(Exchange(), cfg, 15, 3, False, errors)
        x.now = 12345
        book = {"name": "live",
                "positions": {"BTC/EUR": {
                    "market": "BTC/EUR", "qty": 1.0,
                    "stop_order_id": "uncertain-456",
                    "exchange_stop_price": 90.0,
                    "entry_price": 100.0}},
                "risk_status": {"trading_enabled": True},
                "trades": []}
        self.assertIsNone(x.reconcile(book, "BTC/EUR", 90.0, None))
        self.assertFalse(book["risk_status"]["trading_enabled"])
        self.assertEqual(len(book["trades"]), 0)
        self.assertEqual(book["unresolved_order"]["id"], "uncertain-456")


if __name__ == "__main__":
    unittest.main()
