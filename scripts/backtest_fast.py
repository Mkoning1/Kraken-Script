"""15-minute, closed-candle research backtest. No Kraken API keys or live orders.

Fetch real 15m candles from CCXT spot exchanges with historical OHLCV:
  python scripts/backtest_fast.py --download --days 180
Reuse cached data:
  python scripts/backtest_fast.py

This is proxy-exchange research, NOT valid evidence to turn on live Kraken trading.
No optimization is performed on the out-of-sample window and no live configuration
or tuned_params.json is ever updated by this script.
"""
import argparse
import bisect
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.decision import DecisionAgent
from agents.execution import PaperExecutionAgent, plan_stops
from agents.history import HistoryAgent
from agents.indicators import atr, bollinger, ema, macd, rsi, sma
from agents.risk import RiskAgent
from agents.strategies import MarketRegimeAgent, build_agents

TF = 900
HOUR = 3600
FAST_NAMES = ("momentum", "squeeze", "fast_breakout")


def _aggregate_hourly(rows):
    """Only complete, contiguous hourly bars are used; never fabricate missing prices."""
    acc = {}
    for r in rows:
        bucket = r[0] // HOUR * HOUR
        acc.setdefault(bucket, []).append(r)
    result = []
    for start, group in sorted(acc.items()):
        if [r[0] for r in group] != [start + n * TF for n in range(4)]:
            continue
        result.append({
            "ts": start, "open": group[0][1], "high": max(x[2] for x in group),
            "low": min(x[3] for x in group), "close": group[-1][4],
            "volume": sum(x[5] for x in group),
        })
    return result


class MarketSeries:
    """Compute all 15m indicators once. Reference only fully closed 1h candles."""

    def __init__(self, symbol, rows, lookback=24, squeeze_lookback=120):
        self.symbol = symbol
        self.lookback, self.squeeze_lookback = int(lookback), int(squeeze_lookback)
        clean = sorted(rows, key=lambda x: x[0])
        if not clean or any(int(x[0]) % TF for x in clean):
            raise ValueError(f"{symbol}: missing or unaligned 15m timestamps")
        if any(clean[i][0] - clean[i - 1][0] != TF for i in range(1, len(clean))):
            raise ValueError(f"{symbol}: 15m data has gaps; do not pretend bars are consecutive")
        self.rows = clean
        self.times = [x[0] for x in clean]
        self.idx = {ts: i for i, ts in enumerate(self.times)}
        self.high = [float(x[2]) for x in clean]
        self.low = [float(x[3]) for x in clean]
        self.closes = [float(x[4]) for x in clean]
        self.volumes = [float(x[5]) for x in clean]
        self.e20 = ema(self.closes, 20)
        self.e50 = ema(self.closes, 50)
        self.e200 = ema(self.closes, 200)
        self.r = rsi(self.closes, 14)
        self.atr = atr(self.high, self.low, self.closes, 14)
        self.m, self.ms = macd(self.closes)
        self.bbm, self.bbu, self.bbl, self.bbw = bollinger(self.closes)
        self.va = sma(self.volumes, 20)
        hourly = _aggregate_hourly(clean)
        hc = [x["close"] for x in hourly]
        h50, h200 = ema(hc, 50), ema(hc, 200)
        self.trends = {
            x["ts"]: {"close": x["close"], "ema50": h50[i], "ema200": h200[i]}
            for i, x in enumerate(hourly) if i >= 209 and h200[i] is not None
        }

    @staticmethod
    def hour_key(close_ts):
        return (close_ts // HOUR) * HOUR - HOUR

    def trend_at(self, close_ts):
        return self.trends.get(self.hour_key(close_ts))

    def snapshot(self, i):
        if i < max(260, self.lookback + 1, self.squeeze_lookback + 22):
            return None
        fields = (self.e20[i], self.e50[i], self.r[i], self.atr[i],
                  self.m[i], self.ms[i], self.m[i - 1], self.ms[i - 1])
        if any(x is None or not math.isfinite(x) for x in fields):
            return None
        older = self.bbw[i - self.squeeze_lookback - 1:i]
        older = [x for x in older if x is not None]
        prevw = self.bbw[i - 1]
        sq = 50.0 if prevw is None or not older else sum(v < prevw for v in older) / len(older) * 100
        return {
            "close": self.closes[i], "prev_close": self.closes[i - 1],
            "ema20": self.e20[i], "ema50": self.e50[i], "ema200": self.e200[i],
            "rsi": self.r[i], "atr": self.atr[i], "macd": self.m[i],
            "macd_signal": self.ms[i], "prev_macd": self.m[i - 1],
            "prev_macd_signal": self.ms[i - 1],
            "bb_mid": self.bbm[i], "bb_upper": self.bbu[i],
            "bb_lower": self.bbl[i], "squeeze_rank": sq,
            "prev_high": max(self.high[i - self.lookback:i]),
            "volume": self.volumes[i], "volume_avg": self.va[i - 1] or self.va[i],
        }

    def bar(self, index):
        r = self.rows[index]
        return {"ts": int(r[0]), "open": r[1], "high": r[2], "low": r[3],
                "close": r[4], "volume": r[5]}


def new_book(capital):
    return {
        "name": "schaduw", "starting_capital": float(capital), "budget": float(capital),
        "cash": float(capital), "positions": {}, "trades": [], "fees_paid": 0.,
        "equity_history": [], "peak_equity": float(capital),
        "day": {}, "halted": False, "halt_reason": None, "cooldown": {},
    }


class Collector:
    def __init__(self):
        self.trades = []

    def record(self, trade):
        self.trades.append(trade)


def simulate(markets, benchmark, agent, cfg, start, end, capital=1000.):
    """Signal at candle close, execute at NEXT candle open, then check intrabar lows.

    There is no intra-bar profit-taking/stop lookahead. Intrabar stops fill at
    worst of open or stop (plus slip/fee via PaperExecutionAgent).
    """
    profile = cfg["profiles"][cfg["profile"]]
    pcfg = dict(cfg)
    pcfg["slippage_pct"] = max(cfg.get("slippage_pct", 0), cfg.get("backtest", {}).get("slippage_pct", 0.15))
    execution = PaperExecutionAgent(pcfg, 15, profile["cooldown_candles"])
    risk = RiskAgent(profile, cfg["min_order_eur"])
    decider = DecisionAgent([agent], **cfg["decision"])
    regime_agent = MarketRegimeAgent()
    learner = Collector()
    book = new_book(capital)
    pending = {}
    prices = {}
    equity_curve = []
    bar_times = sorted(set.union(*(set(c.times) for c in markets.values())))
    event_count = signal_count = 0
    for ts in bar_times:
        if ts + TF < start or ts >= end:
            continue
        active = {sym: (ms, ms.idx[ts]) for sym, ms in markets.items() if ts in ms.idx}
        if not active:
            continue
        # Orders are created from signals generated at the PREVIOUS 15m close.
        for sym, (ms, i) in sorted(active.items()):
            bar = ms.bar(i)
            cmd = pending.pop(sym, None)
            if cmd and cmd["action"] == "exit" and sym in book["positions"]:
                execution.sell(book, sym, bar["open"], ts, "Next-open signal exit", learner)
                event_count += 1
            if cmd and cmd["action"] == "buy" and sym not in book["positions"]:
                mark = {s: prices.get(s, v.closes[v.idx.get(ts, 0)]) for s, v in markets.items()}
                equity = book["cash"] + sum(p["qty"] * mark.get(s, p["entry_price"])
                                            for s, p in book["positions"].items())
                status, _ = risk.update(book, equity, ts)
                price = float(bar["open"])
                stop, hard, extra = plan_stops(cmd["signal"], price, cmd["context"], cfg["exits"], cfg["agents"][agent.name])
                extra = dict(extra, _tf_sec=TF, _cooldown_sec=profile["cooldown_candles"] * TF)
                exposure = sum(p["qty"] * mark.get(s, p["entry_price"]) for s, p in book["positions"].items())
                ok, qty, risk_eur, _ = risk.approve_entry(
                    book, status, equity, exposure, sym, price, hard, book["cash"],
                    cmd["score"], ts, None)
                if ok:
                    execution.buy(book, sym, agent.name, cmd["signal"], qty, price, stop, hard, extra,
                                  risk_eur, ts - TF, "Next-open entry", cmd["score"], {})
                    event_count += 1
            # Only then process the current bar's stop. This deliberately permits
            # entry and stop inside the same following bar.
            if sym in book["positions"]:
                execution.manage(book, sym, [bar], None, learner, bar["close"])
            prices[sym] = float(bar["close"])

        equity = book["cash"] + sum(p["qty"] * prices.get(sym, p["entry_price"])
                                    for sym, p in book["positions"].items())
        status, halt = risk.update(book, equity, ts + TF)
        if halt:
            for sym in list(book["positions"]):
                execution.sell(book, sym, prices[sym], ts + TF, "Portfolio drawdown stop", learner)
            pending.clear()
        equity_curve.append((ts + TF, book["cash"] + sum(p["qty"] * prices.get(sym, p["entry_price"])
                                                           for sym, p in book["positions"].items())))
        # Signals use only information known at this candle's close.
        benchmark_trend = benchmark.trend_at(ts + TF)
        if not benchmark_trend or not notional_ok(benchmark_trend):
            continue
        regime, _ = regime_agent.assess(benchmark_trend)
        mult = cfg["regime_multiplier"][regime]
        for sym, (ms, i) in active.items():
            trend = ms.trend_at(ts + TF)
            snap = ms.snapshot(i)
            if not snap or not trend or not notional_ok(trend):
                continue
            sig = agent.analyse({"sig": snap, "trend": trend, "h4": None})
            if sym in book["positions"]:
                if sig.action == "sell" and sig.strength >= 0.5:
                    pending[sym] = {"action": "exit"}
                continue
            if sig.action != "buy":
                continue
            signal_count += 1
            decisions = decider.entries({agent.name: sig}, {agent.name: 1.0}, mult, regime)
            chosen = next((x for x in decisions if x["type"] == "entry"), None)
            if chosen:
                pending[sym] = {"action": "buy", "signal": sig, "score": chosen["score"],
                                "context": {"sig": snap, "trend": trend, "h4": None}}

    # Do NOT pretend to liquidate at the last observed price. Value remaining
    # inventory at the last closed bar instead.
    final = book["cash"] + sum(p["qty"] * prices.get(sym, p["entry_price"])
                               for sym, p in book["positions"].items())
    peak = capital
    mdd = 0.
    for _, v in equity_curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100 if peak else 0)
    trades = learner.trades
    rs = [x["r"] for x in trades]
    win = sum(x["pnl"] > 0 for x in trades)
    # The benchmark is held over the SAME time window and proxy-market candles.
    btc_close = [benchmark.closes[i] for i, ts in enumerate(benchmark.times)
                 if start <= ts + TF < end]
    hold = (btc_close[-1] / btc_close[0] - 1) * 100 if len(btc_close) >= 2 else None
    return {
        "trades": len(trades), "signals": signal_count, "events": event_count,
        "open_positions_end": len(book["positions"]),
        "win_rate_pct": round(100 * win / len(trades), 2) if trades else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "return_pct": round((final / capital - 1) * 100, 2),
        "max_drawdown_pct": round(mdd, 2),
        "fees_paid": round(book["fees_paid"], 2),
        "btc_hold_pct": round(hold, 2) if hold is not None else None,
    }


def notional_ok(trend):
    return all(trend.get(k) is not None and math.isfinite(float(trend[k]))
               for k in ("close", "ema50", "ema200"))


def run(download=False, days=180, markets=None, min_days=60):
    cfg = json.loads((ROOT / "config.json").read_text())
    symbols = markets or ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR", "LINK/EUR"]
    symbols = list(dict.fromkeys(["BTC/EUR"] + symbols))
    history = HistoryAgent(ROOT / "data" / "history_15m", timeframe="15m")
    series = {}
    failures = {}
    for symbol in symbols:
        base = symbol.split("/")[0]
        try:
            if download:
                history.update(base, days)
            rows = history.load(base)
            if len(rows) < min_days * 96:
                raise ValueError(f"only {len(rows)} candles, requires {min_days * 96}")
            series[symbol] = MarketSeries(
                symbol, rows,
                lookback=cfg["agents"]["fast_breakout"].get("lookback", 24),
                squeeze_lookback=cfg["agents"]["squeeze"].get("squeeze_lookback", 120))
        except Exception as exc:
            failures[symbol] = str(exc)
            print(f"SKIP {symbol}: {exc}", file=sys.stderr)
    report = {"generated_ts": int(time.time()), "timeframe_minutes": 15,
              "source": "CCXT spot proxy markets; not necessarily Kraken EUR",
              "no_live_promotion": True, "failed_markets": failures,
              "strategies": {}, "approved_for_live": False}
    if "BTC/EUR" not in series or len(series) < 2:
        report["validation"] = "insufficient_data"
    else:
        aligned_min = max(s.times[0] for s in series.values())
        aligned_max = min(s.times[-1] + TF for s in series.values())
        if aligned_max - aligned_min < min_days * 86400:
            report["validation"] = "insufficient_overlap"
        else:
            split = aligned_min + int((aligned_max - aligned_min) * 0.6) // TF * TF
            report["window"] = {"start": aligned_min, "split": split, "end": aligned_max}
            fast_agents = {a.name: a for a in build_agents(cfg["agents"]) if a.name in FAST_NAMES}
            for name, agent in fast_agents.items():
                train = simulate(series, series["BTC/EUR"], agent, cfg, aligned_min, split)
                oos = simulate(series, series["BTC/EUR"], agent, cfg, split, aligned_max)
                report["strategies"][name] = {"train": train, "oos": oos}
            report["validation"] = "research_only"
    out = ROOT / "data" / "fast_backtest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--min-days", type=int, default=60)
    ap.add_argument("--markets", nargs="+", default=None)
    args = ap.parse_args()
    report = run(args.download, args.days, args.markets, args.min_days)
    # Insufficient evidence means validation must not be treated as passing.
    return 0 if report["validation"] == "research_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
