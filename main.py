"""Multi-agent trader v3 voor Kraken. Elke run (elke 15 minuten via GitHub Actions):

  1. Data-agent         kiest de munten (top op volume) en haalt 15-min-, uur- en zo nodig 4-uurscandles op
  2. Uitvoer-agents     bewaken open posities: stop-losses, trailing stops, controle op Kraken
  3. Risico-agent       daglimiet en noodstop, apart voor echt geld en schaduwgeld
  4. Markt-agent        stijgende, neutrale of dalende markt (BTC)
  5. Prestatie-agent    invloed per agent, en wie met echt geld mag handelen
  6. Strategie-agents   Trend-4u (jouw bot) + vier korte-termijnagents geven hun advies
  7. Beslis-agent       rangschikt alle kansen
  8. Risico-agent       keurt goed of af en bepaalt de inzet
  9. Uitvoer-agents     kopen: echt geld via Kraken, schaduwgeld op papier
 10. Monitor-agent      verslag, logboek en dashboard
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from agents.data import DataAgent
from agents.decision import DecisionAgent
from agents.execution import LiveExecutionAgent, PaperExecutionAgent, plan_stops
from agents.fmt import eur, px
from agents.indicators import correlation, h4_series, snapshot, snapshot_4h, trend_snapshot
from agents.learning import LearningAgent
from agents.monitor import MonitorAgent, book_equity, book_exposure
from agents.risk import RiskAgent
from agents.strategies import MarketRegimeAgent, build_agents

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "data" / "state.json"
DASHBOARD_PATH = ROOT / "docs" / "data.json"
LEGACY_STATE = ROOT / "bot_state.json"
STATE_VERSION = 3
TF = 60          # signalen en bewaking op uurcandles
BACKTEST_PATH = ROOT / "data" / "backtest.json"


def load_config():
    cfg = json.loads((ROOT / "config.json").read_text())
    if cfg["mode"] not in ("live", "paper"):
        raise SystemExit("mode moet 'live' of 'paper' zijn")
    if cfg["profile"] not in cfg["profiles"]:
        raise SystemExit(f"Profiel '{cfg['profile']}' bestaat niet. Kies uit: {', '.join(cfg['profiles'])}")
    tuned_path = ROOT / "data" / "tuned_params.json"
    if cfg.get("backtest", {}).get("use_tuned_params") and tuned_path.exists():
        for name, params in json.loads(tuned_path.read_text()).items():
            if name == "_universe_size":
                cfg["universe"]["size"] = int(params)  # aantal munten dat de backtest het beste vond
            elif name in cfg["agents"]:
                cfg["agents"][name].update(params)  # instellingen die de backtest heeft bewezen
    return cfg


def use_everything(cfg):
    return str(cfg["live"]["budget_eur"]).lower() == "alles"


def budget_number(cfg):
    return 0.0 if use_everything(cfg) else float(cfg["live"]["budget_eur"])


def new_book(name, capital):
    return {"name": name, "starting_capital": float(capital), "budget": float(capital), "cash": float(capital),
            "positions": {}, "trades": [], "fees_paid": 0.0, "equity_history": [], "peak_equity": float(capital),
            "day": {}, "halted": False, "halt_reason": None, "cooldown": {}}


def migrate_legacy(state, cfg, now_ts):
    """Neemt de open positie en historie van je oude bot over (bot_state.json)."""
    if not LEGACY_STATE.exists():
        return
    old = json.loads(LEGACY_STATE.read_text())
    state["legacy_trades"] = old.get("trade_history", [])
    live = state["books"].get("live")
    if not live or not old.get("in_position"):
        return
    sym, qty, entry = old["symbol"], float(old["position_size"]), float(old["entry_price"])
    atr = float(old.get("current_atr") or entry * 0.05)
    peak = float(old.get("peak_price") or entry)
    runner = bool(old.get("is_runner"))
    stop = peak - (6.0 if runner else 2.0) * atr
    cost = qty * entry * (1 + cfg["fee_pct"] / 100)
    try:
        entry_ts = int(datetime.fromisoformat(old["entry_time"]).timestamp())
    except Exception:
        entry_ts = now_ts
    live["positions"][sym] = {
        "market": sym, "agent": "trend4h", "book": "live", "qty": qty, "entry_price": entry, "entry_cost": cost,
        "entry_ts": entry_ts, "exit_style": "two_stage", "stop": stop, "initial_stop": entry - 2 * atr,
        "hard_stop": stop - cfg["exits"]["catastrophe_atr"] * atr, "risk_eur": qty * 3 * atr, "r_unit": 2 * atr,
        "trailing": True, "trail_distance": 2 * atr, "max_candles": 0, "candles_held": 0, "highest_close": peak,
        "last_checked_ts": now_ts, "atr": atr, "peak": peak, "is_runner": runner,
        "last_4h_ts": int((old.get("last_candle_ts") or {}).get(sym, 0) // 1000),
        "initial_atr": 2.0, "runner_atr": 6.0, "runner_threshold_atr": 3.0,
        "entry_reason": "Overgenomen van je oude bot (4-uursuitbraak)", "score": 1.0, "plan": None,
    }
    live["cash"] = max(0.0, live["budget"] - cost)
    state["events"].append({"ts": now_ts, "type": "note", "book": "live", "market": sym,
                            "reason": f"Positie {sym} overgenomen van je oude bot: {qty:g} stuks, instap {px(entry)}, "
                                      f"piek {px(peak)}, {'runner' if runner else 'krappe stop'}"})


def load_state(cfg, now_ts):
    if STATE_PATH.exists():
        st = json.loads(STATE_PATH.read_text())
        if st.get("version") == STATE_VERSION:
            if cfg["mode"] == "live" and not st["books"].get("live"):
                st["books"]["live"] = new_book("live", budget_number(cfg))
            return st
    st = {"version": STATE_VERSION, "started_ts": now_ts,
          "books": {"live": new_book("live", budget_number(cfg)) if cfg["mode"] == "live" else None,
                    "schaduw": new_book("schaduw", cfg["shadow"]["capital_eur"])},
          "agent_status": {}, "agent_stats": {}, "last_candle_ts": {}, "last_4h_scan": None,
          "events": [], "evaluations": {}, "errors": [], "narrative": [], "last_run": {}}
    migrate_legacy(st, cfg, now_ts)
    return st


def persist(state):
    """Direct opslaan na elke echte order, zodat een crash later in de run nooit een positie 'vergeet'."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False))


def make_exchange(cfg):
    import ccxt
    key, secret = os.environ.get("KRAKEN_API_KEY", ""), os.environ.get("KRAKEN_API_SECRET", "")
    opts = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if cfg["mode"] == "live":
        if not key or not secret:
            raise SystemExit("KRAKEN_API_KEY / KRAKEN_API_SECRET ontbreken (GitHub Secrets)")
        opts.update(apiKey=key, secret=secret)
    return ccxt.kraken(opts)


def run(now_ts=None, exchange=None):
    now_ts = now_ts or int(time.time())
    cfg = load_config()
    profile = cfg["profiles"][cfg["profile"]]
    state = load_state(cfg, now_ts)
    ex = exchange or make_exchange(cfg)
    validate = cfg["live"]["validate_only"]
    errors, events, evaluations = [], [], {}

    def ev(**kw):
        kw.setdefault("ts", now_ts)
        events.append(kw)

    # Budget ophogen als je dat in config.json verhoogt
    live = state["books"].get("live")
    if live and not use_everything(cfg) and budget_number(cfg) > live["budget"]:
        extra = cfg["live"]["budget_eur"] - live["budget"]
        live["budget"] += extra
        live["cash"] += extra
        live["starting_capital"] += extra
        ev(type="note", book="live", reason=f"Budget verhoogd met {eur(extra)}")

    if live and not live.get("initialized"):
        try:
            free = float(ex.fetch_balance()["free"].get("EUR", 0) or 0)
            invested = sum(p["entry_cost"] for p in live["positions"].values())
            live["cash"] = free if use_everything(cfg) else max(0.0, min(live["budget"] - invested, free))
            if use_everything(cfg):
                live["budget"] = live["cash"] + invested
            start_value = live["cash"] + invested  # rendement telt vanaf wat je erin stopte
            live["starting_capital"] = live["peak_equity"] = start_value
            live["initialized"] = True
            ev(type="note", book="live", reason=f"Echt-geldboek gestart: {eur(live['cash'])} vrij te gebruiken"
                                                + (" (heel je vrije Kraken-saldo)" if use_everything(cfg) else f" (budget {eur(live['budget'])}, vrij op Kraken {eur(free)})"))
        except Exception as e:
            errors.append(f"Saldo ophalen bij start mislukt: {e}")

    strategies = build_agents(cfg["agents"])
    labels = {a.name: a.label for a in strategies}
    backtest = {}
    if BACKTEST_PATH.exists():
        try:
            backtest = json.loads(BACKTEST_PATH.read_text()).get("agents", {})
        except Exception as e:
            errors.append(f"Backtest-resultaten lezen mislukt: {e}")
    learner = LearningAgent(state, cfg["learning"], cfg["promotion"], cfg["agents"], backtest, cfg["backtest"]["min_trades"])
    decider = DecisionAgent(strategies, **cfg["decision"])
    risk = RiskAgent(profile, cfg["min_order_eur"])
    paper = PaperExecutionAgent(cfg, TF, profile["cooldown_candles"])
    live_exec = LiveExecutionAgent(ex, cfg, TF, profile["cooldown_candles"], validate, errors) if live else None
    execs = {"live": live_exec, "schaduw": paper}
    if live_exec:
        live_exec.now = now_ts
        perms = state.get("permissions")
        if validate or not perms or not all(perms.values()):  # opnieuw tot alle rechten in orde zijn
            try:
                state["permissions"] = live_exec.check_permissions()
                missing = [n for n, ok in state["permissions"].items() if not ok]
                if missing:
                    errors.append("API-sleutel mist rechten: " + ", ".join(missing))
            except Exception as e:
                errors.append(f"Rechtencheck mislukt: {e}")
    monitor = MonitorAgent(STATE_PATH, DASHBOARD_PATH)
    data = DataAgent(ex, cfg)

    # 1. Data
    held = {m for b in state["books"].values() if b for m in b["positions"]}
    try:
        markets, prices, _ = data.universe(held)
    except Exception as e:
        markets, prices = list(dict.fromkeys(list(cfg["universe"]["always_include"]) + list(held))), {}
        errors.append(f"Marktlijst ophalen mislukt ({e}), alleen vaste munten")
    fast, slow, h4list, h4 = {}, {}, {}, {}
    for m in markets:
        try:
            c1 = data.candles(m, 60, now_ts)
            if not DataAgent.is_fresh(c1, 60, now_ts):
                raise RuntimeError("koersdata is verouderd")
            c4 = data.candles(m, 240, now_ts)
            fast[m], slow[m] = c1, c4
            h4list[m] = h4_series(c4)
            prices.setdefault(m, c1[-1]["close"])
        except Exception as e:
            errors.append(f"{m}: {e}")
    # Elke munt waarvan sinds de vorige beoordeling een 4-uurscandle gesloten is (ook na een gemiste run)
    lookback = cfg["agents"].get("trend4h", {}).get("lookback", 55)
    last4 = state.setdefault("last_4h_eval", {})
    for m, c4 in slow.items():
        if c4 and c4[-1]["ts"] > last4.get(m, 0):
            try:
                h4[m] = snapshot_4h(c4, lookback)
            except ValueError:
                pass  # te nieuw op Kraken: nog niet genoeg historie
            except Exception as e:
                errors.append(f"{m} (4 uur): {e}")
    if h4:
        state["last_4h_scan"] = now_ts
    for b in state["books"].values():
        if b:
            for m, p in b["positions"].items():
                prices.setdefault(m, p["entry_price"])

    # 2. Open posities bewaken
    exited = {"live": set(), "schaduw": set()}
    for name, book in state["books"].items():
        if not book:
            continue
        for m in list(book["positions"]):
            try:
                res = execs[name].manage(book, m, fast.get(m, []), h4list.get(m), learner, prices.get(m))
            except Exception as e:
                errors.append(f"{m} ({'echt geld' if name == 'live' else 'schaduw'}): bewaken mislukt: {e}")
                continue
            if name == "live":
                persist(state)
            if res and res.get("validated"):
                ev(type="validated", book=name, market=m, reason=res["reason"])
            elif res:
                exited[name].add(m)
                ev(type="exit", book=name, market=m, price=res["exit_price"], pnl=round(res["pnl"], 2),
                   agent=labels.get(res["agent"], res["agent"]), reason=res["exit_reason"], ts=res["exit_ts"])

    # Heel je saldo in gebruik: boek gelijktrekken met Kraken en stortingen of opnames herkennen
    if live and use_everything(cfg) and live.get("initialized"):
        try:
            free = float(live_exec.balance(refresh=True)["free"].get("EUR", 0) or 0)
            diff = free - live["cash"]
            if abs(diff) > 0.5:
                live["starting_capital"] += diff
                live["peak_equity"] += diff
                live["budget"] += diff
                if live["day"].get("start_equity") is not None:
                    live["day"]["start_equity"] += diff
                ev(type="note", book="live", reason=f"{'Storting' if diff > 0 else 'Opname'} van {eur(abs(diff))} herkend op Kraken; telt niet als winst of verlies")
            live["cash"] = free
        except Exception as e:
            errors.append(f"Saldo gelijktrekken mislukt: {e}")

    # 3. Daglimiet en noodstop
    for name, book in state["books"].items():
        if not book:
            continue
        enabled = cfg["live"]["trading_enabled"] if name == "live" else True
        status, halt = risk.update(book, book_equity(book, prices), now_ts, enabled)
        book["risk_status"] = status
        if halt:
            ev(type="note", book=name, reason=f"NOODSTOP ({'echt geld' if name == 'live' else 'schaduw'}): {book['halt_reason']}")
            for m in list(book["positions"]):
                try:
                    res = execs[name].sell(book, m, prices[m], now_ts, "Noodstop: alles verkocht", learner)
                    if res and not res.get("validated"):
                        ev(type="exit", book=name, market=m, price=res["exit_price"], pnl=round(res["pnl"], 2),
                           agent=labels.get(res["agent"]), reason=res["exit_reason"])
                except Exception as e:
                    errors.append(f"{m}: noodverkoop mislukt: {e}")

    # 4. Markt-agent
    try:
        regime, regime_reason = MarketRegimeAgent().assess(trend_snapshot(slow[cfg["regime_market"]]))
    except Exception as e:
        regime, regime_reason = "neutraal", f"Onbekend ({e}), neutraal aangenomen"
    regime_mult = cfg["regime_multiplier"][regime]

    # 5. Prestatie-agent
    for note in learner.review(labels):
        ev(type="note", reason=note)
    weights = {a.name: learner.weight(a.name) for a in strategies}

    # 6-7. Adviezen en beslissingen
    candidates = []
    for m, c15 in fast.items():  # c15 = uurcandles
        last_ts = c15[-1]["ts"]
        if state["last_candle_ts"].get(m) == last_ts and m not in h4:
            continue
        state["last_candle_ts"][m] = last_ts
        try:
            ctx = {"sig": snapshot(c15, cfg), "trend": trend_snapshot(slow[m]), "h4": h4.get(m)}
        except ValueError:
            continue  # te nieuw op Kraken: nog niet genoeg historie
        except Exception as e:
            errors.append(f"{m}: {e}")
            continue
        if m in h4:
            last4[m] = h4[m]["ts"]
        signals = {a.name: a.analyse(ctx) for a in strategies}
        close_ts = last_ts + TF * 60
        notes = []
        for name, book in state["books"].items():
            if not book or m not in book["positions"]:
                continue
            d = decider.exit_check(signals, book["positions"][m])
            notes.append(f"{'Echt geld' if name == 'live' else 'Schaduw'}: {d['reason']}")
            if d["type"] == "exit":
                try:
                    res = execs[name].sell(book, m, ctx["sig"]["close"], close_ts, d["reason"], learner)
                    if name == "live":
                        persist(state)
                    if res.get("validated"):
                        ev(type="validated", book=name, market=m, reason=res["reason"])
                    else:
                        exited[name].add(m)
                        ev(type="exit", book=name, market=m, price=res["exit_price"], pnl=round(res["pnl"], 2),
                           agent=labels.get(res["agent"]), reason=d["reason"], ts=close_ts)
                except Exception as e:
                    errors.append(f"{m}: verkopen mislukt: {e}")
        for d in decider.entries(signals, weights, regime_mult, regime):
            if d["type"] != "entry":
                notes.append(d["reason"])
                continue
            bname = "live" if learner.status(d["agent"]) == "live" and state["books"].get("live") else "schaduw"
            book = state["books"][bname]
            if m in book["positions"] or m in exited[bname]:
                notes.append(f"{d['reason']} (al een positie of net verkocht)")
                continue
            candidates.append((d, bname, m, ctx, last_ts))
            notes.append(f"Kandidaat voor {'echt geld' if bname == 'live' else 'schaduw'}: {d['reason']} ({d['calc']})")
        evaluations[m] = {"ts": close_ts, "price": ctx["sig"]["close"],
                          "signals": {labels[n]: s.to_dict() for n, s in signals.items()},
                          "notes": notes or ["Geen actie"]}

    # 8-9. Beste kansen eerst langs de risico-agent
    for d, bname, m, ctx, last_ts in sorted(candidates, key=lambda x: -x[0]["score"]):
        book = state["books"][bname]
        if m in book["positions"]:
            evaluations[m]["notes"].append(f"{d['reason']}: overgeslagen, deze run al gekocht door een andere agent")
            continue
        price = ctx["sig"]["close"]
        stop, hard, extra = plan_stops(d["signal"], price, ctx, cfg["exits"], cfg["agents"].get(d["agent"], {}))
        equity = book_equity(book, prices)
        avail = book["cash"]
        if bname == "live":
            try:
                avail = min(avail, live_exec.free_eur())
            except Exception as e:
                errors.append(f"Saldo ophalen mislukt: {e}")
                continue
        corr = None
        for held_m in book["positions"]:
            if held_m in slow and m in slow:
                c = correlation(slow[m], slow[held_m])
                if c is not None and (corr is None or c > corr[0]):
                    corr = (c, held_m)
        ok, qty, risk_eur, why = risk.approve_entry(book, book["risk_status"], equity, book_exposure(book, prices),
                                                    m, price, hard, avail, d["score"], last_ts, corr)
        plan = {"stop": stop, "hard_stop": hard, "risk_eur": round(risk_eur, 2), "calc": d["calc"], "risk": why,
                "max_value": equity * profile["max_position_pct"] / 100}
        where = "echt geld" if bname == "live" else "schaduw"
        if not ok:
            evaluations[m]["notes"].append(f"Risico-agent ({where}): {why}")
            if not why.startswith(("Maximum van", "Te weinig ruimte")):
                ev(type="blocked", book=bname, market=m, price=price, reason=f"{d['reason']}. Tegengehouden: {why}")
            continue
        try:
            res = execs[bname].buy(book, m, d["agent"], d["signal"], qty, price, stop, hard, extra, risk_eur,
                                   last_ts, d["reason"], d["score"], plan)
        except Exception as e:
            errors.append(f"{m}: kopen mislukt: {e}")
            continue
        finally:
            if bname == "live":
                persist(state)
        evaluations[m]["notes"].append(f"Uitvoer-agent ({where}): {res['text']}")
        if res.get("ok"):
            ev(type="entry", book=bname, market=m, price=book["positions"][m]["entry_price"], agent=labels[d["agent"]],
               reason=f"{d['reason']}. {d['calc']}. {why} {res['text']}", ts=last_ts + TF * 60)
        elif res.get("validated"):
            ev(type="validated", book=bname, market=m, reason=f"{d['reason']}. {why} {res['text']}")
        else:
            ev(type="blocked", book=bname, market=m, reason=f"{d['reason']}. {res['text']}")

    # 10. Opslaan
    state["evaluations"].update(evaluations)
    state["events"] += events
    state["errors"] += [{"ts": now_ts, "message": e} for e in errors]
    regime_info = {"label": regime, "reason": regime_reason, "multiplier": regime_mult}
    state["last_run"] = {"ts": now_ts, "ok": not errors, "regime": regime_info,
                         "prices": {m: prices[m] for m in prices if any(b and m in b["positions"] for b in state["books"].values()) or m == cfg["benchmark_market"]}}
    state["narrative"] = MonitorAgent.narrative(state, cfg, regime_info, len(fast), events, bool(h4), errors, validate)
    monitor.record(state, now_ts, prices, prices.get(cfg["benchmark_market"]))
    monitor.save(state, cfg, profile, prices, learner.summary(strategies),
                 {"markets": markets, "spark": {m: [c["close"] for c in fast[m][-48:]] for m in fast}})

    for line in state["narrative"]:
        print(line)
    for e in errors:
        print("  LET OP:", e)
    return state


if __name__ == "__main__":
    run()
    sys.exit(0)
