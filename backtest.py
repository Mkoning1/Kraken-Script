"""Backtest-lab. Laat elke strategie-agent twee jaar historie 'naspelen' met precies dezelfde regels,
kosten en stops als in het echt, en zoekt per agent betere instellingen.

Eerlijk optimaliseren (walk-forward):
  1. De periode wordt gesplitst: de eerste 60% om te zoeken, de laatste 40% om te controleren.
  2. Per agent worden een paar instellingen geprobeerd op het eerste deel; de beste wint.
  3. Die winnaar wordt getest op het laatste deel, dat hij nooit gezien heeft.
  4. Alleen als hij daar ook winst maakt (en niet slechter is dan de standaard), wordt hij overgenomen.
Zo voorkomen we instellingen die alleen toevallig goed waren in het verleden.

Gebruik:  python backtest.py            (historie bijwerken en testen)
          python backtest.py --no-download
"""
import itertools
import json
import sys
import time
from pathlib import Path

from agents.decision import DecisionAgent
from agents.execution import PaperExecutionAgent, plan_stops
from agents.history import HistoryAgent
from agents.indicators import atr, atr_sma, bollinger, ema, macd, rsi, rsi_sma, sma
from agents.risk import RiskAgent
from agents.strategies import AGENT_CLASSES, MarketRegimeAgent

ROOT = Path(__file__).parent
H, H4 = 3600, 14400
SKIP_KEYS = {"start", "force", "enabled"}

# Instellingen die geprobeerd worden (klein gehouden: minder kans op toevalstreffers)
GRIDS = {
    "trend4h": {"lookback": [30, 55, 80], "initial_atr": [2.0, 2.5], "runner_atr": [4.0, 6.0]},
    "mean_reversion": {"rsi_buy": [22, 28, 34], "stop_atr": [1.5, 2.5], "max_candles": [8, 16]},
    "breakout": {"lookback": [12, 24, 48], "volume_mult": [1.0, 1.5], "stop_atr": [2.0, 3.0]},
    "momentum": {"volume_mult": [1.0, 1.5, 2.0], "stop_atr": [1.5, 2.5]},
    "squeeze": {"squeeze_pct": [5, 10, 20], "stop_atr": [1.5, 2.5]},
}


class Collector:
    def __init__(self):
        self.trades = []

    def record(self, t):
        self.trades.append(t)


def aggregate_4h(rows):
    out = {}
    for r in rows:
        k = r[0] - r[0] % H4
        o = out.get(k)
        if o is None:
            out[k] = [k, r[1], r[2], r[3], r[4], r[5], 1]
        else:
            o[2] = max(o[2], r[2]); o[3] = min(o[3], r[3]); o[4] = r[4]; o[5] += r[5]; o[6] += 1
    return [v[:6] for k, v in sorted(out.items()) if v[6] == 4]


class CoinData:
    """Alle indicatoren van één munt, één keer vooraf berekend (zelfde formules als live)."""

    def __init__(self, market, rows):
        self.market = market
        self.ts = [r[0] for r in rows]
        self.o, self.h, self.l, self.c, self.v = ([r[k] for r in rows] for k in range(1, 6))
        self.idx = {t: i for i, t in enumerate(self.ts)}
        self.e20, self.e50, self.e200 = ema(self.c, 20), ema(self.c, 50), ema(self.c, 200)
        self.rsi, self.atr = rsi(self.c, 14), atr(self.h, self.l, self.c, 14)
        self.macd, self.macd_sig = macd(self.c)
        self.bb_mid, self.bb_up, self.bb_low, self.bb_w = bollinger(self.c, 20, 2.0)
        self.vol_avg = sma(self.v, 20)
        r4 = aggregate_4h(rows)
        self.t4 = [r[0] for r in r4]
        self.h4h, self.l4, self.c4 = [r[2] for r in r4], [r[3] for r in r4], [r[4] for r in r4]
        self.idx4 = {t: j for j, t in enumerate(self.t4)}
        self.e50_4, self.e200_4 = ema(self.c4, 50), ema(self.c4, 200)
        self.rsi4, self.atr4 = rsi_sma(self.c4, 14), atr_sma(self.h4h, self.l4, self.c4, 14)
        self._ph, self._sq, self._don = {}, {}, {}

    def prev_high(self, lb):
        if lb not in self._ph:
            arr = [None] * len(self.h)
            for i in range(lb, len(self.h)):
                arr[i] = max(self.h[i - lb:i])
            self._ph[lb] = arr
        return self._ph[lb]

    def squeeze_rank(self, look):
        if look not in self._sq:
            w, arr = self.bb_w, [None] * len(self.bb_w)
            for i in range(look + 2, len(w)):
                prev = w[i - 1]
                win = [x for x in w[i - look:i] if x is not None]
                if prev is not None and win:
                    arr[i] = sum(1 for x in win if x < prev) / len(win) * 100
            self._sq[look] = arr
        return self._sq[look]

    def donchian(self, lb):
        if lb not in self._don:
            arr = [None] * len(self.h4h)
            for j in range(lb, len(self.h4h)):
                arr[j] = max(self.h4h[j - lb:j])
            self._don[lb] = arr
        return self._don[lb]

    def sig(self, i, lb, sq):
        if i < 260:
            return None
        ph, sr = self.prev_high(lb)[i], self.squeeze_rank(sq)[i]
        vals = (self.e200[i], self.rsi[i], self.atr[i], self.macd_sig[i], self.macd_sig[i - 1], self.bb_mid[i], ph, sr)
        if any(x is None for x in vals):
            return None
        return {"close": self.c[i], "prev_close": self.c[i - 1], "ema20": self.e20[i], "ema50": self.e50[i],
                "ema200": self.e200[i], "rsi": self.rsi[i], "atr": self.atr[i], "macd": self.macd[i],
                "macd_signal": self.macd_sig[i], "prev_macd": self.macd[i - 1], "prev_macd_signal": self.macd_sig[i - 1],
                "bb_mid": self.bb_mid[i], "bb_upper": self.bb_up[i], "bb_lower": self.bb_low[i], "squeeze_rank": sr,
                "prev_high": ph, "volume": self.v[i], "volume_avg": self.vol_avg[i - 1] or self.vol_avg[i]}

    def last4(self, close_t):
        """Index van de laatste gesloten 4-uurscandle op moment close_t."""
        return self.idx4.get(close_t - close_t % H4 - H4)

    def trend(self, close_t):
        j = self.last4(close_t)
        if j is None or j < 210 or self.e200_4[j] is None:
            return None
        return {"close": self.c4[j], "ema50": self.e50_4[j], "ema200": self.e200_4[j]}

    def h4_snap(self, j, lb):
        don = self.donchian(lb)[j]
        if j < lb + 16 or don is None or self.rsi4[j] is None or not self.atr4[j]:
            return None
        return {"ts": self.t4[j], "close": self.c4[j], "donchian_high": don, "rsi": self.rsi4[j], "atr": self.atr4[j]}


def new_book(capital):
    return {"name": "schaduw", "starting_capital": capital, "budget": capital, "cash": capital, "positions": {},
            "trades": [], "fees_paid": 0.0, "equity_history": [], "peak_equity": capital, "day": {},
            "halted": False, "halt_reason": None, "cooldown": {}}


def simulate(coins, specs, cfg, profile, t0, t1, halts=False, capital=1000.0):
    """Speel de periode t0-t1 uur voor uur na met de gegeven agents en instellingen."""
    agents, params_of = [], {}
    for name, params in specs:
        a = AGENT_CLASSES[name](**params)
        a.use_regime = params.get("use_regime", True)
        agents.append(a)
        params_of[name] = params
    lb = params_of.get("breakout", cfg["agents"]["breakout"]).get("lookback", 24)
    sq = params_of.get("squeeze", cfg["agents"]["squeeze"]).get("squeeze_lookback", 120)
    lb4 = params_of.get("trend4h", cfg["agents"]["trend4h"]).get("lookback", 55)
    decider = DecisionAgent(agents, **cfg["decision"])
    risk = RiskAgent(profile, cfg["min_order_eur"])
    pcfg = dict(cfg, slippage_pct=cfg["backtest"]["slippage_pct"])
    paper = PaperExecutionAgent(pcfg, 60, profile["cooldown_candles"])
    book, col, weights = new_book(capital), Collector(), {a.name: 1.0 for a in agents}
    regime_agent, btc = MarketRegimeAgent(), coins.get(cfg["regime_market"])
    last_price, curve, halts_n, paused_until = {}, [], 0, 0

    def equity():
        return book["cash"] + sum(p["qty"] * last_price.get(m, p["entry_price"]) for m, p in book["positions"].items())

    for t in range(t0 - t0 % H, t1, H):
        close_t = t + H
        new4 = close_t % H4 == 0
        for m, cd in coins.items():
            i = cd.idx.get(t)
            if i is None:
                continue
            last_price[m] = cd.c[i]
            if m in book["positions"]:
                h4l = []
                if new4:
                    j = cd.idx4.get(close_t - H4)
                    if j is not None and cd.atr4[j]:
                        h4l = [{"ts": cd.t4[j], "close": cd.c4[j], "atr": cd.atr4[j]}]
                paper.manage(book, m, [{"ts": t, "open": cd.o[i], "high": cd.h[i], "low": cd.l[i], "close": cd.c[i]}],
                             h4l, col, cd.c[i])
        eq = equity()
        status, halt = risk.update(book, eq, close_t, True)
        if halt:
            if halts:
                halts_n += 1
                for m in list(book["positions"]):
                    paper.sell(book, m, last_price[m], close_t, "Noodstop", col)
                paused_until = close_t + 7 * 86400
            book["halted"] = False
            book["peak_equity"] = equity()
        if t % 86400 == 0:
            curve.append((t, round(equity(), 2)))
        if close_t < paused_until:
            continue
        tr = btc.trend(close_t) if btc else None
        regime = regime_agent.assess(tr)[0] if tr else "neutraal"
        mult = cfg["regime_multiplier"][regime]
        cands = []
        for m, cd in coins.items():
            i = cd.idx.get(t)
            if i is None:
                continue
            sig = cd.sig(i, lb, sq)
            trend = cd.trend(close_t) if sig else None
            if not trend:
                continue
            h4 = None
            if new4:
                j = cd.idx4.get(close_t - H4)
                if j is not None:
                    h4 = cd.h4_snap(j, lb4)
            ctx = {"sig": sig, "trend": trend, "h4": h4}
            signals = {a.name: a.analyse(ctx) for a in agents}
            pos = book["positions"].get(m)
            if pos:
                d = decider.exit_check(signals, pos)
                if d["type"] == "exit":
                    paper.sell(book, m, sig["close"], close_t, d["reason"], col)
                continue
            for d in decider.entries(signals, weights, mult, regime):
                if d["type"] == "entry":
                    cands.append((d, m, ctx))
        for d, m, ctx in sorted(cands, key=lambda x: -x[0]["score"]):
            if m in book["positions"]:
                continue
            price = ctx["sig"]["close"]
            stop, hard, extra = plan_stops(d["signal"], price, ctx, cfg["exits"], params_of[d["agent"]])
            eq = equity()
            expo = eq - book["cash"]
            ok, qty, risk_eur, _ = risk.approve_entry(book, status, eq, expo, m, price, hard, book["cash"], d["score"], t)
            if ok:
                paper.buy(book, m, d["agent"], d["signal"], qty, price, stop, hard, extra, risk_eur, t, d["reason"], d["score"], {})

    final = equity()
    trades = col.trades
    rs = [x["r"] for x in trades]
    wins = [x for x in trades if x["pnl"] > 0]
    gw, gl = sum(x["pnl"] for x in wins), -sum(x["pnl"] for x in trades if x["pnl"] < 0)
    peak, mdd = 0, 0
    for _, v in curve + [(t1, final)]:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100 if peak else 0)
    return {
        "trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
        "profit_factor": round(gw / gl, 2) if gl else None, "return_pct": round((final / capital - 1) * 100, 1),
        "max_drawdown_pct": round(mdd, 1), "fees": round(book["fees_paid"], 2), "halts": halts_n,
        "curve": curve[::7],
    }


def defaults_for(cfg, name):
    return {k: v for k, v in cfg["agents"][name].items() if k not in SKIP_KEYS}


def run(download=True, log=print):
    started = time.time()
    cfg = json.loads((ROOT / "config.json").read_text())
    profile = cfg["profiles"][cfg["profile"]]
    bt = cfg["backtest"]
    dash = ROOT / "docs" / "data.json"
    markets = json.loads(dash.read_text()).get("markets", []) if dash.exists() else []
    markets = list(dict.fromkeys(list(cfg["universe"]["always_include"]) + markets))[:16]
    hist = HistoryAgent(ROOT / "data" / "history", log)
    coins = {}
    for m in markets:
        base = m.split("/")[0]
        if download:
            try:
                hist.update(base, bt["days"])
            except Exception as e:
                log(f"{base}: historie bijwerken mislukt ({e})")
        rows = hist.load(base)
        if len(rows) > 24 * 60:
            coins[m] = CoinData(m, rows)
    if cfg["regime_market"] not in coins:
        raise SystemExit("Geen historie voor de marktmaatstaf (BTC); backtest kan niet draaien")
    t1 = min(cd.ts[-1] for cd in coins.values()) + H
    t0 = max(t1 - bt["days"] * 86400, min(cd.ts[0] for cd in coins.values()) + 40 * 86400)
    split = t0 + int((t1 - t0) * bt["train_share"])
    log(f"{len(coins)} munten, periode {time.strftime('%Y-%m-%d', time.gmtime(t0))} tot {time.strftime('%Y-%m-%d', time.gmtime(t1))}")

    results, tuned = {}, {}
    for name in cfg["agents"]:
        if name not in AGENT_CLASSES or not cfg["agents"][name].get("enabled", True):
            continue
        base_p = defaults_for(cfg, name)
        full = simulate(coins, [(name, base_p)], cfg, profile, t0, t1)
        base_oos = simulate(coins, [(name, base_p)], cfg, profile, split, t1)
        best, best_score, tried = None, None, 0
        grid = GRIDS.get(name, {})
        for combo in itertools.product(*grid.values()):
            p = dict(base_p, **dict(zip(grid.keys(), combo)))
            r = simulate(coins, [(name, p)], cfg, profile, t0, split)
            tried += 1
            score = r["total_r"] if r["trades"] >= 20 else None
            if score is not None and (best_score is None or score > best_score):
                best, best_score = p, score
        chosen, chosen_oos, adopted = base_p, base_oos, False
        if best and best != base_p:
            oos = simulate(coins, [(name, best)], cfg, profile, split, t1)
            if (oos["expectancy_r"] or -1) > 0 and oos["trades"] >= 10 and (oos["expectancy_r"] or -1) >= (base_oos["expectancy_r"] or -1):
                chosen, chosen_oos, adopted = best, oos, True
                tuned[name] = {k: best[k] for k in grid}
        if adopted:
            full = simulate(coins, [(name, chosen)], cfg, profile, t0, t1)
        results[name] = {**{k: v for k, v in full.items() if k != "curve"},
                         "oos_trades": chosen_oos["trades"], "oos_expectancy_r": chosen_oos["expectancy_r"],
                         "oos_return_pct": chosen_oos["return_pct"], "default_oos_expectancy_r": base_oos["expectancy_r"],
                         "tuned": adopted, "params": {k: chosen[k] for k in grid}, "tried": tried}
        log(f"{name}: {full['trades']} trades, gem. {full['expectancy_r']}R, rendement {full['return_pct']}%, "
            f"controleperiode {chosen_oos['expectancy_r']}R" + (" (nieuwe instellingen)" if adopted else ""))

    team_specs = [(n, dict(defaults_for(cfg, n), **tuned.get(n, {}))) for n in results]
    team = simulate(coins, team_specs, cfg, profile, t0, t1, halts=True)
    btc = coins[cfg["regime_market"]]
    i0 = next(i for i, t in enumerate(btc.ts) if t >= t0)
    out = {
        "generated_ts": int(time.time()), "seconds": round(time.time() - started),
        "period": {"start": t0, "end": t1, "split": split}, "coins": list(coins),
        "sources": hist.meta, "profile": cfg["profile"], "fee_pct": cfg["fee_pct"], "slippage_pct": bt["slippage_pct"],
        "agents": results, "team": team, "btc_return_pct": round((btc.c[-1] / btc.c[i0] - 1) * 100, 1),
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "backtest.json").write_text(json.dumps(out, indent=1))
    (ROOT / "data" / "tuned_params.json").write_text(json.dumps(tuned, indent=1))
    (ROOT / "docs" / "backtest.json").write_text(json.dumps(out, separators=(",", ":")))
    log(f"Team: {team['trades']} trades, rendement {team['return_pct']}%, grootste daling {team['max_drawdown_pct']}%, "
        f"BTC vasthouden {out['btc_return_pct']}%. Klaar in {out['seconds']} s.")
    return out


if __name__ == "__main__":
    lines = []
    def log(msg):
        print(msg, flush=True)
        lines.append(f"{time.strftime('%H:%M:%S')} {msg}")
    try:
        run(download="--no-download" not in sys.argv, log=log)
    except BaseException as e:
        import traceback
        log("MISLUKT: " + "".join(traceback.format_exception(e))[-1500:])
        raise
    finally:
        (ROOT / "data").mkdir(exist_ok=True)
        (ROOT / "data" / "backtest_log.txt").write_text("\n".join(lines))
