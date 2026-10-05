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


def daily_volume(cd):
    """Handelsvolume in de quote-munt per dag: {dag: volume}."""
    out = {}
    for t, c, v in zip(cd.ts, cd.c, cd.v):
        d = t - t % 86400
        out[d] = out.get(d, 0.0) + c * v
    return out


def universe_by_day(coins, size, always):
    """Net als de live Data-agent: elke dag de munten met het meeste volume van dat moment (7 dagen terugkijkend).
    Zo komt een munt pas in beeld als hij op dat moment druk verhandeld wordt, niet omdat we achteraf weten dat hij steeg."""
    vols = {m: daily_volume(cd) for m, cd in coins.items()}
    days = sorted({d for v in vols.values() for d in v})
    uni = {}
    for d in days:
        ranked = []
        for m, v in vols.items():
            week = sum(v.get(d - k * 86400, 0.0) for k in range(1, 8))  # alleen dagen die al voorbij zijn
            if week > 0 and v.get(d - 86400) is not None:
                ranked.append((week, m))
        ranked.sort(reverse=True)
        chosen = {m for _, m in ranked[:size]}
        chosen |= {m for m in always if m in coins}
        uni[d] = chosen
    return uni


def simulate(coins, specs, cfg, profile, t0, t1, universe, halts=False, capital=1000.0, exclude=()):
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
        active = universe.get(t - t % 86400, set()) - set(exclude)
        for m in list(book["positions"]):
            cd = coins[m]
            i = cd.idx.get(t)
            if i is None:
                continue
            last_price[m] = cd.c[i]
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
            book["halt_until"] = None
            book["peak_equity"] = equity()
        if t % 86400 == 0:
            curve.append((t, round(equity(), 2)))
        if close_t < paused_until:
            continue
        tr = btc.trend(close_t) if btc else None
        regime = regime_agent.assess(tr)[0] if tr else "neutraal"
        mult = cfg["regime_multiplier"][regime]
        cands = []
        for m in active | set(book["positions"]):
            cd = coins[m]
            i = cd.idx.get(t)
            if i is None:
                continue
            last_price[m] = cd.c[i]
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
            if m not in active:
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
    by_m = {}
    for x in trades:
        b = by_m.setdefault(x["market"], {"trades": 0, "r": 0.0})
        b["trades"] += 1
        b["r"] = round(b["r"] + x["r"], 3)
    best = max(by_m, key=lambda m: by_m[m]["r"]) if by_m else None
    rest = [x["r"] for x in trades if x["market"] != best]
    return {
        "trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
        "profit_factor": round(gw / gl, 2) if gl else None, "return_pct": round((final / capital - 1) * 100, 1),
        "max_drawdown_pct": round(mdd, 1), "fees": round(book["fees_paid"], 2), "halts": halts_n,
        "best_market": best, "best_market_r": by_m[best]["r"] if best else None,
        "ex_best_expectancy_r": round(sum(rest) / len(rest), 3) if rest else None,
        "robust_r": round(sum(rest), 2),
        "by_market": by_m, "curve": curve[::7],
    }


def defaults_for(cfg, name):
    return {k: v for k, v in cfg["agents"][name].items() if k not in SKIP_KEYS}


def build_pool(cfg, log):
    """Munten om uit te kiezen: een vaste lijst van gevestigde munten plus de huidige drukste op Kraken."""
    pool = list(cfg["universe"]["always_include"]) + [f"{b}/EUR" for b in cfg["backtest"]["pool_fixed"]]
    try:
        import ccxt
        ex = ccxt.kraken({"enableRateLimit": True})
        ex.load_markets()
        tick = ex.fetch_tickers()
        excl = set(cfg["universe"]["exclude_bases"])
        ranked = sorted(((t.get("quoteVolume") or 0, s) for s, t in tick.items()
                         if s.endswith("/EUR") and s.split("/")[0] not in excl and ex.markets.get(s, {}).get("spot", True)), reverse=True)
        pool += [s for _, s in ranked[:cfg["backtest"]["pool_kraken_top"]]]
    except Exception as e:
        log(f"Kraken-lijst ophalen mislukt ({str(e)[:80]}), alleen de vaste lijst")
    return list(dict.fromkeys(pool))


def run(download=True, log=print):
    started = time.time()
    cfg = json.loads((ROOT / "config.json").read_text())
    profile = cfg["profiles"][cfg["profile"]]
    bt = cfg["backtest"]
    hist = HistoryAgent(ROOT / "data" / "history", log)
    pool = build_pool(cfg, log) if download else [p.name + "/EUR" for p in (ROOT / "data" / "history").iterdir() if p.is_dir()]
    coins = {}
    for m in pool:
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
    t1 = coins[cfg["regime_market"]].ts[-1] + H
    t0 = max(t1 - bt["days"] * 86400, coins[cfg["regime_market"]].ts[0] + 40 * 86400)
    split = t0 + int((t1 - t0) * bt["train_share"])
    always = cfg["universe"]["always_include"]
    sizes = bt.get("universe_sizes", [cfg["universe"]["size"]])
    universes = {n: universe_by_day(coins, n, always) for n in sizes}
    base_n = cfg["universe"]["size"] if cfg["universe"]["size"] in universes else sizes[0]
    log(f"{len(coins)} munten in de pool, periode {time.strftime('%Y-%m-%d', time.gmtime(t0))} tot {time.strftime('%Y-%m-%d', time.gmtime(t1))}")

    def score(r):  # robuust: de winst zonder de beste munt telt
        return r["robust_r"] if r["trades"] >= 20 else None

    results, tuned = {}, {}
    for name in cfg["agents"]:
        if name not in AGENT_CLASSES or not cfg["agents"][name].get("enabled", True):
            continue
        tf_minutes = int(cfg["agents"][name].get("timeframe_minutes", 240 if name == "trend4h" else 60))
        if tf_minutes == 15:
            log(f"{name}: overgeslagen in legacy backtest (historie is 1u; live agent draait 15m)")
            continue
        uni = universes[base_n]
        base_p = defaults_for(cfg, name)
        base_oos = simulate(coins, [(name, base_p)], cfg, profile, split, t1, uni)
        best, best_score, tried = None, None, 0
        grid = GRIDS.get(name, {})
        for combo in itertools.product(*grid.values()):
            p = dict(base_p, **dict(zip(grid.keys(), combo)))
            r = simulate(coins, [(name, p)], cfg, profile, t0, split, uni)
            tried += 1
            sc = score(r)
            if sc is not None and (best_score is None or sc > best_score):
                best, best_score = p, sc
        chosen, chosen_oos, adopted = base_p, base_oos, False
        if best and best != base_p:
            oos = simulate(coins, [(name, best)], cfg, profile, split, t1, uni)
            if (oos["expectancy_r"] or -1) > 0 and oos["trades"] >= 10 and (oos["expectancy_r"] or -1) >= (base_oos["expectancy_r"] or -1):
                chosen, chosen_oos, adopted = best, oos, True
                tuned[name] = {k: best[k] for k in grid}
        full = simulate(coins, [(name, chosen)], cfg, profile, t0, t1, uni)
        ex = simulate(coins, [(name, chosen)], cfg, profile, t0, t1, uni, exclude=[full["best_market"]]) if full["best_market"] else full
        results[name] = {**{k: v for k, v in full.items() if k not in ("curve",)},
                         "ex_best_return_pct": ex["return_pct"], "ex_best_trades": ex["trades"],
                         "oos_trades": chosen_oos["trades"], "oos_expectancy_r": chosen_oos["expectancy_r"],
                         "oos_return_pct": chosen_oos["return_pct"], "oos_ex_best_expectancy_r": chosen_oos["ex_best_expectancy_r"],
                         "default_oos_expectancy_r": base_oos["expectancy_r"],
                         "tuned": adopted, "params": {k: chosen[k] for k in grid}, "tried": tried, "universe_size": base_n,
                         "timeframe_minutes": tf_minutes}
        log(f"{name}: {full['trades']} trades, gem. {full['expectancy_r']}R, rendement {full['return_pct']}%; "
            f"zonder beste munt ({full['best_market']}) {ex['return_pct']}%; controleperiode {chosen_oos['expectancy_r']}R"
            + (" (nieuwe instellingen)" if adopted else ""))

    # Meer of minder munten? Getest voor de agent(s) die met echt geld handelen.
    size_test = {}
    live_names = [n for n in results if cfg["agents"][n].get("start") == "live" or cfg["agents"][n].get("force") == "live"]
    if len(sizes) > 1 and live_names:
        n0 = live_names[0]
        p0 = dict(defaults_for(cfg, n0), **tuned.get(n0, {}))
        for n in sizes:
            tr = simulate(coins, [(n0, p0)], cfg, profile, t0, split, universes[n])
            oo = simulate(coins, [(n0, p0)], cfg, profile, split, t1, universes[n])
            size_test[n] = {"train_robust_r": tr["robust_r"], "train_return_pct": tr["return_pct"],
                            "oos_expectancy_r": oo["expectancy_r"], "oos_ex_best_expectancy_r": oo["ex_best_expectancy_r"],
                            "oos_return_pct": oo["return_pct"], "oos_trades": oo["trades"]}
            log(f"{n0} met {n} munten: leerperiode zonder beste munt {tr['robust_r']}R, controleperiode {oo['expectancy_r']}R, {oo['return_pct']}%")
        best_n = max(size_test, key=lambda n: size_test[n]["train_robust_r"])
        if bt.get("auto_universe_size", False) and best_n != base_n and (size_test[best_n]["oos_expectancy_r"] or -1) > 0 \
                and (size_test[best_n]["oos_expectancy_r"] or -1) >= (size_test[base_n]["oos_expectancy_r"] or -1):
            tuned["_universe_size"] = best_n
            log(f"Aantal munten wordt {best_n}: beter in de leerperiode en bevestigd in de controleperiode")

    team_n = tuned.get("_universe_size", base_n)
    team_specs = [(n, dict(defaults_for(cfg, n), **tuned.get(n, {}))) for n in results]
    team = simulate(coins, team_specs, cfg, profile, t0, t1, universes[team_n], halts=True)
    live_only = simulate(coins, [(n, dict(defaults_for(cfg, n), **tuned.get(n, {}))) for n in live_names],
                         cfg, profile, t0, t1, universes[team_n], halts=True) if live_names else None
    btc = coins[cfg["regime_market"]]
    i0 = next(i for i, t in enumerate(btc.ts) if t >= t0)
    out = {
        "generated_ts": int(time.time()), "seconds": round(time.time() - started),
        "period": {"start": t0, "end": t1, "split": split}, "coins": list(coins), "pool_size": len(coins),
        "universe_size": team_n, "size_test": size_test,
        "sources": hist.meta, "profile": cfg["profile"], "fee_pct": cfg["fee_pct"], "slippage_pct": bt["slippage_pct"],
        "agents": results, "team": {k: v for k, v in team.items() if k != "by_market"},
        "live_team": {k: v for k, v in live_only.items() if k != "by_market"} if live_only else None,
        "btc_return_pct": round((btc.c[-1] / btc.c[i0] - 1) * 100, 1),
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "backtest.json").write_text(json.dumps(out, indent=1))
    (ROOT / "data" / "tuned_params.json").write_text(json.dumps(tuned, indent=1))
    (ROOT / "docs" / "backtest.json").write_text(json.dumps(out, separators=(",", ":")))
    if live_only:
        log(f"Echt-geldteam ({', '.join(live_names)}): rendement {live_only['return_pct']}%, grootste daling {live_only['max_drawdown_pct']}%, "
            f"{live_only['halts']} noodstop(s).")
    log(f"Hele team: {team['trades']} trades, rendement {team['return_pct']}%. BTC vasthouden {out['btc_return_pct']}%. Klaar in {out['seconds']} s.")
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
