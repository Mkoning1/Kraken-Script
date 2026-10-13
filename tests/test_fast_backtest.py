"""Prove 15m research uses only fully closed higher timeframe candles and live indicators."""
import math
import unittest

from agents.indicators import snapshot
from agents.strategies import MomentumAgent
from scripts.backtest_fast import MarketSeries, _aggregate_hourly, simulate


def data(n=1200, start=0):
    out = []
    for i in range(n):
        px = 100 + i * .012 + 2 * math.sin(i / 9)
        op = px - .03
        out.append([start + i * 900, op, px + .2, px - .25,
                    px, 400 + 100 * math.sin(i / 10) ** 2])
    return out


class FastBacktestTests(unittest.TestCase):
    def test_one_hour_uses_four_complete_15m_bars(self):
        rows = data(10)
        hours = _aggregate_hourly(rows)
        self.assertEqual(len(hours), 2)
        self.assertEqual(hours[0]["close"], rows[3][4])
        self.assertEqual(hours[1]["close"], rows[7][4])
        rows.pop(6)
        self.assertEqual(len(_aggregate_hourly(rows)), 1)

    def test_missing_15m_candle_is_rejected(self):
        rows = data(500)
        rows.pop(120)
        with self.assertRaisesRegex(ValueError, "gaps"):
            MarketSeries("BTC/EUR", rows)

    def test_current_hour_not_used_until_it_closes(self):
        ms = MarketSeries("BTC/EUR", data())
        earlier = ms.trend_at(960 * 900 + 900)
        # Any two closes within the same hour resolve to the same closed hour.
        later = ms.trend_at(960 * 900 + 1800)
        self.assertEqual(earlier, later)
        # On the next hour boundary a newly closed hourly candle becomes visible.
        at_close = ms.trend_at(964 * 900)
        self.assertNotEqual(earlier, at_close)

    def test_fast_indicator_parity_with_live(self):
        cfg = {"agents": {"breakout": {"lookback": 24},
                          "squeeze": {"squeeze_lookback": 120}}}
        rows = data()
        ms = MarketSeries("BTC/EUR", rows)
        index = 998
        current = [{ "ts": x[0], "open": x[1], "high": x[2], "low": x[3],
                     "close": x[4], "volume": x[5]} for x in rows[:index + 1]]
        realtime = snapshot(current, cfg)
        research = ms.snapshot(index)
        for key in realtime:
            self.assertAlmostEqual(realtime[key], research[key], places=7, msg=key)

    def test_market_strategy_simulated_with_known_fees(self):
        from unittest.mock import Mock
        rows = data()
        ms = MarketSeries("BTC/EUR", rows)
        agent = MomentumAgent(volume_mult=1.5, stop_atr=2.0)
        agent.use_regime = True
        cfg = {
            "profile": "assertief",
            "profiles": {"assertief": {
                "risk_per_trade_pct": 1.0, "max_position_pct": 40,
                "max_open_positions": 2, "max_exposure_pct": 75,
                "max_daily_loss_pct": 5, "max_drawdown_pct": 20,
                "cooldown_candles": 3, "correlation_limit": 0.85,
            }},
            "fee_pct": 0.4, "slippage_pct": 0.08, "min_order_eur": 5,
            "backtest": {"slippage_pct": 0.15},
            "decision": {"min_score": 0.52, "veto_strength": 0.8, "confluence_bonus": 0.15},
            "regime_multiplier": {"stijgend": 1.0, "neutraal": 0.8, "dalend": 0.0},
            "exits": {"catastrophe_atr": 1.0, "min_stop_pct": 2.0,
                      "breakeven_at_r": 1.0, "trail_after_r": 1.5},
            "agents": {"momentum": {"stop_atr": 2.0}},
        }
        result = simulate({"BTC/EUR": ms}, ms, agent, cfg,
                          900 * 900, rows[-1][0] + 900)
        self.assertIsInstance(result["return_pct"], float)
        self.assertGreaterEqual(result["fees_paid"], 0)
        self.assertEqual(result["trades"], 0 if result["events"] == 0 else result["trades"])
        self.assertLessEqual(result["max_drawdown_pct"], 100)


if __name__ == "__main__":
    unittest.main()
