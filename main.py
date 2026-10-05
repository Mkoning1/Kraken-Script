"""Multi-agent trader v4 voor Kraken. Elke run (elke 5 minuten via GitHub Actions):

  1. Data-agent         scant breed elke 5 min; discovery elke 15 min; haalt 15m/1u/4u-data per desk op
  2. Uitvoer-agents     bewaken open posities: stop-losses, trailing stops, controle op Kraken
  3. Risico-agent       daglimiet en noodstop, apart voor echt geld en schaduwgeld
  4. Markt-agent        stijgende, neutrale of dalende markt (BTC)
  5. Prestatie-agent    invloed per agent, en wie met echt geld mag handelen
  6. Strategie-agents   Fast desk (15m), swing desk (1u) en Trend-4u (4u) geven hun advies
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
TF = 15          # fallback voor nieuwe posities; elke positie bewaart voortaan zijn eigen timeframe
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

    # 1. Data + multi-speed opportunity scanner
    cadence = cfg.get("cadence", {})
    held = {m for b in state["books"].values() if b for m in b["positions"]}
    scan_report = {"enabled": False, "candidates": [], "core_markets": [], "discovery_markets": []}
    try:
        # Eén ticker-batch: brede markt- en prijscheck iedere 5 minuten.
        proposed_markets, _, _ = data.universe(held)
        scan_report = data.scan_report
    except Exception as e:
        proposed_markets = list(dict.fromkeys(list(cfg["universe"]["always_include"]) + list(held)))
        errors.append(f"Marktlijst ophalen mislukt ({e}), alleen vaste munten")

    fast_tf = int(cadence.get("fast_minutes", 15))
    swing_tf = int(cadence.get("swing_minutes", 60))
    trend_tf = int(cadence.get("trend_minutes", 240))
    discovery_tf = int(cadence.get("discovery_minutes", 15))

    def is_new(slot_name, minutes):
        last = state.get(slot_name)
        return last is None or now_ts // (minutes * 60) > int(last) // (minutes * 60)

    new_discovery = is_new("last_discovery_scan", discovery_tf)
    new_fast = is_new("last_fast_scan", fast_tf)
    new_hour = is_new("last_hour_scan", swing_tf)
    new_4h = is_new("last_4h_scan", trend_tf)

    # Discovery-lijst verandert alleen op kwartiergrenzen. Tussendoor blijven posities + actieve set stabiel.
    if new_discovery or not state.get("active_markets"):
        markets = list(proposed_markets)
        state["active_markets"] = list(markets)
        state["discovery_markets"] = list(scan_report.get("discovery_markets", []))
        state["last_discovery_scan"] = now_ts
    else:
        markets = list(dict.fromkeys(list(state.get("active_markets", proposed_markets)) + list(held)))
    discovery_markets = set(state.get("discovery_markets", []))

    prices = {m: data.all_prices[m] for m in markets if m in data.all_prices}
    c15, c1, c4, h4list, h4 = {}, {}, {}, {}, {}
    lookback = cfg["agents"].get("trend4h", {}).get("lookback", 55)
    last4 = state.setdefault("last_4h_eval", {})
    levels = state.setdefault("levels", {})
    trend1_cache = state.setdefault("trend_1h_cache", {})
    trend4_cache = state.setdefault("trend_4h_cache", {})
    corr4_cache = state.setdefault("corr_4h_cache", {})

    # Fast desk: alleen iedere gesloten 15m-candle zware 15m-data ophalen.
    if new_fast:
        for m in markets:
            try:
                candles = data.candles(m, 15, now_ts)
                if not DataAgent.is_fresh(candles, 15, now_ts):
                    raise RuntimeError("15m-koersdata is verouderd")
                c15[m] = candles
                prices.setdefault(m, candles[-1]["close"])
            except Exception as e:
                errors.append(f"{m} (15m): {e}")
        state["last_fast_scan"] = now_ts
        if c15:
            state["spark"] = {m: [c["close"] for c in rows[-96:]] for m, rows in c15.items()}

    # Swing desk + 1u trendcache. Eerste run na een upgrade vult de cache ook als het uur nog niet omsloeg.
    need_hour = new_hour or any(m not in trend1_cache for m in markets)
    if need_hour:
        for m in markets:
            try:
                candles = data.candles(m, 60, now_ts)
                if not DataAgent.is_fresh(candles, 60, now_ts):
                    raise RuntimeError("1u-koersdata is verouderd")
                c1[m] = candles
                trend1_cache[m] = trend_snapshot(candles)
                prices.setdefault(m, candles[-1]["close"])
            except ValueError:
                continue
            except Exception as e:
                errors.append(f"{m} (1u): {e}")
        state["last_hour_scan"] = now_ts

    # Trend desk + 4u cache/correlatie. Dit gebeurt alleen bij een nieuwe 4u-candle of lege cache.
    need_4h = new_4h or any(m not in trend4_cache for m in markets)
    if need_4h:
        for m in markets:
            try:
                candles = data.candles(m, 240, now_ts)
                if not DataAgent.is_fresh(candles, 240, now_ts):
                    raise RuntimeError("4u-koersdata is verouderd")
                c4[m] = candles
                h4list[m] = h4_series(candles)
                corr4_cache[m] = [{"ts": x["ts"], "close": x["close"]} for x in candles[-100:]]
                try:
                    trend4_cache[m] = trend_snapshot(candles)
                except ValueError:
                    pass
                if len(candles) >= lookback:
                    levels[m] = {"breakout": max(x["high"] for x in candles[-lookback:]), "ts": candles[-1]["ts"]}
                if candles[-1]["ts"] > last4.get(m, 0):
                    try:
                        h4[m] = snapshot_4h(candles, lookback)
                    except ValueError:
                        pass
                prices.setdefault(m, candles[-1]["close"])
            except Exception as e:
                errors.append(f"{m} (4u): {e}")
        state["last_4h_scan"] = now_ts

    for book in state["books"].values():
        if book:
            for m, p in book["positions"].items():
                prices.setdefault(m, data.all_prices.get(m, p["entry_price"]))

    agent_map = {a.name: a for a in strategies}

    # 2. Open posities iedere 5 minuten bewaken/reconciliëren.
    # Live stops staan op Kraken zelf. Schaduwposities krijgen daarnaast een 5m ticker-stopcheck.
    exited = {"live": set(), "schaduw": set()}
    for name, book in state["books"].items():
        if not book:
            continue
        for m in list(book["positions"]):
            pos = book["positions"].get(m)
            if not pos:
                continue
            owner = agent_map.get(pos.get("agent"))
            horizon = getattr(owner, "horizon", "swing") if owner else "swing"
            series = c15.get(m, []) if horizon == "fast" else c1.get(m, []) if horizon == "swing" else []
            four = h4list.get(m) if pos.get("exit_style") == "two_stage" else None
            last_price = prices.get(m)

            if name == "schaduw" and last_price:
                ticker_stop = pos.get("hard_stop") if pos.get("exit_style") == "two_stage" else pos.get("stop")
                if ticker_stop and last_price <= ticker_stop:
                    try:
                        res = execs[name].sell(book, m, last_price, now_ts, "5-minuten stopcontrole geraakt", learner)
                        exited[name].add(m)
                        ev(type="exit", book=name, market=m, price=res["exit_price"], pnl=round(res["pnl"], 2),
                           agent=labels.get(res["agent"], res["agent"]), reason=res["exit_reason"], ts=res["exit_ts"])
                    except Exception as e:
                        errors.append(f"{m} (schaduw): 5m-stopcontrole mislukt: {e}")
                    continue

            try:
                res = execs[name].manage(book, m, series, four, learner, last_price)
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

    # 4. Markt-agent: 1u-regime, gecachet tussen uurgrenzen.
    btc_trend = trend1_cache.get(cfg["regime_market"])
    if btc_trend:
        try:
            regime, regime_reason = MarketRegimeAgent().assess(btc_trend)
        except Exception as e:
            regime, regime_reason = "neutraal", f"Onbekend ({e}), neutraal aangenomen"
    else:
        previous = state.get("last_run", {}).get("regime") or {}
        regime = previous.get("label", "neutraal")
        regime_reason = previous.get("reason", "Nog geen bruikbare 1u-trend beschikbaar")
    regime_mult = cfg["regime_multiplier"][regime]

    # 5. Prestatie-agent
    for note in learner.review(labels):
        ev(type="note", reason=note)
    weights = {a.name: learner.weight(a.name) for a in strategies}

    # 6-7. Adviezen en beslissingen per desk.
    candidates = []
    signal_seen = state.setdefault("last_signal_ts", {})

    def merge_evaluation(market, close_ts, price, signals, notes):
        row = evaluations.setdefault(market, {"ts": close_ts, "price": price, "signals": {}, "notes": []})
        row["ts"] = max(row.get("ts", 0), close_ts)
        row["price"] = price
        row["signals"].update({labels[n]: sig.to_dict() for n, sig in signals.items()})
        row["notes"].extend(notes or ["Geen actie"])

    def process_desk(horizon, series_map, trend_cache, tf_minutes):
        desk_agents = [a for a in strategies if a.horizon == horizon]
        if not desk_agents:
            return
        for m, candles in series_map.items():
            if not candles:
                continue
            last_ts = candles[-1]["ts"]
            seen_key = f"{horizon}:{m}"
            if signal_seen.get(seen_key) == last_ts:
                continue
            signal_seen[seen_key] = last_ts

            try:
                if horizon == "trend":
                    hs = h4.get(m)
                    if not hs:
                        continue
                    sig_snapshot = {"close": hs["close"], "atr": hs["atr"]}
                    ctx = {"sig": sig_snapshot, "trend": trend_cache.get(m) or {}, "h4": hs}
                    price = hs["close"]
                else:
                    trend = trend_cache.get(m)
                    if not trend:
                        continue
                    ctx = {"sig": snapshot(candles, cfg), "trend": trend, "h4": h4.get(m)}
                    price = ctx["sig"]["close"]
            except ValueError:
                continue
            except Exception as e:
                errors.append(f"{m} ({horizon}): {e}")
                continue

            signals = {a.name: a.analyse(ctx) for a in desk_agents}
            close_ts = last_ts + tf_minutes * 60
            notes = []

            # Alleen de eigenaar-agent van een positie mag hem op strategiesignaal sluiten.
            for name, book in state["books"].items():
                if not book or m not in book["positions"]:
                    continue
                pos = book["positions"][m]
                owner = agent_map.get(pos.get("agent"))
                if not owner or owner.horizon != horizon:
                    continue
                d = decider.exit_check(signals, pos)
                notes.append(f"{'Echt geld' if name == 'live' else 'Schaduw'}: {d['reason']}")
                if d["type"] == "exit":
                    try:
                        res = execs[name].sell(book, m, price, close_ts, d["reason"], learner)
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
                if m in discovery_markets and cfg.get("scanner", {}).get("shadow_only", True):
                    bname = "schaduw"
                    notes.append("Opportunity scanner: discovery-markt blijft voorlopig schaduw-only")
                book = state["books"][bname]
                if m in book["positions"] or m in exited[bname]:
                    notes.append(f"{d['reason']} (al een positie of net verkocht)")
                    continue
                candidates.append((d, bname, m, ctx, last_ts, tf_minutes, price))
                notes.append(f"Kandidaat voor {'echt geld' if bname == 'live' else 'schaduw'}: {d['reason']} ({d['calc']})")

            merge_evaluation(m, close_ts, price, signals, notes)

    # Fast desk krijgt iedere 15m Momentum + Squeeze + Snelle uitbraak.
    if new_fast:
        process_desk("fast", c15, trend1_cache, 15)

    # Swing desk krijgt ieder uur Dip-koper + normale Uitbraak, bevestigd door 4u-trend.
    if new_hour:
        process_desk("swing", c1, trend4_cache, 60)

    # Trend desk alleen op een nieuwe gesloten 4u-candle.
    if h4:
        process_desk("trend", c4, trend4_cache, 240)
        for m, hs in h4.items():
            last4[m] = hs["ts"]

    # 8-9. Beste kansen over alle desks eerst langs de risico-agent.
    for d, bname, m, ctx, last_ts, tf_minutes, signal_price in sorted(candidates, key=lambda x: -x[0]["score"]):
        book = state["books"][bname]
        if m in book["positions"]:
            evaluations[m]["notes"].append(f"{d['reason']}: overgeslagen, deze run al gekocht door een andere desk")
            continue

        price = signal_price
        stop, hard, extra = plan_stops(d["signal"], price, ctx, cfg["exits"], cfg["agents"].get(d["agent"], {}))
        extra = dict(extra or {})
        extra["_tf_sec"] = tf_minutes * 60
        extra["_cooldown_sec"] = profile["cooldown_candles"] * tf_minutes * 60

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
            if held_m in corr4_cache and m in corr4_cache:
                c = correlation(corr4_cache[m], corr4_cache[held_m])
                if c is not None and (corr is None or c > corr[0]):
                    corr = (c, held_m)

        ok, qty, risk_eur, why = risk.approve_entry(
            book, book["risk_status"], equity, book_exposure(book, prices),
            m, price, hard, avail, d["score"], last_ts, corr
        )
        plan = {
            "desk": d["horizon"], "timeframe_minutes": tf_minutes,
            "stop": stop, "hard_stop": hard, "risk_eur": round(risk_eur, 2),
            "calc": d["calc"], "risk": why,
            "max_value": equity * profile["max_position_pct"] / 100,
        }
        where = "echt geld" if bname == "live" else "schaduw"
        if not ok:
            evaluations[m]["notes"].append(f"Risico-agent ({where}): {why}")
            if not why.startswith(("Maximum van", "Te weinig ruimte")):
                ev(type="blocked", book=bname, market=m, price=price, reason=f"{d['reason']}. Tegengehouden: {why}")
            continue
        try:
            res = execs[bname].buy(
                book, m, d["agent"], d["signal"], qty, price, stop, hard, extra, risk_eur,
                last_ts, d["reason"], d["score"], plan
            )
        except Exception as e:
            errors.append(f"{m}: kopen mislukt: {e}")
            continue
        finally:
            if bname == "live":
                persist(state)
        evaluations[m]["notes"].append(f"Uitvoer-agent ({where}, {tf_minutes}m): {res['text']}")
        if res.get("ok"):
            ev(type="entry", book=bname, market=m, price=book["positions"][m]["entry_price"], agent=labels[d["agent"]],
               reason=f"{d['reason']}. {d['calc']}. {why} {res['text']}", ts=last_ts + tf_minutes * 60)
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
    state["narrative"] = MonitorAgent.narrative(state, cfg, regime_info, len(markets), events, bool(h4), errors, validate)
    if scan_report.get("enabled"):
        active_discovery = sorted(discovery_markets)
        state["narrative"].insert(
            1,
            f"Opportunity scanner: {scan_report.get('pool_count', 0)} liquide EUR-markten voorselecteerd; "
            f"{len(active_discovery)} extra kans(en) draaien {'alleen in schaduw' if scan_report.get('shadow_only') else 'mee in de handel'}."
        )
    scan_report = dict(scan_report)
    scan_report["active_markets"] = list(markets)
    scan_report["active_discovery_markets"] = sorted(discovery_markets)

    # Compact scanlog: iedere worker-run wordt vastgelegd, ook als er geen trade is.
    # We bewaren zeven dagen op 5-minutencadans (max. 2016 scans) in de operationele state.
    scan_history = state.setdefault("scan_history", [])
    top_scan = []
    for row in (scan_report.get("candidates") or [])[:5]:
        top_scan.append({
            "market": row.get("market"),
            "score": round(float(row.get("score", 0)) * 100, 1),
            "change_pct": round(float(row.get("change_pct", 0)), 2),
            "selected": bool(row.get("selected")),
        })
    scan_history.append({
        "ts": now_ts,
        "ok": not errors,
        "eligible": int(scan_report.get("eligible_count", 0) or 0),
        "ranked": int(scan_report.get("pool_count", 0) or 0),
        "active": len(markets),
        "discovery": len(discovery_markets),
        "top": top_scan,
        "regime": regime,
        "live_positions": len(state["books"].get("live", {}).get("positions", {}) if state["books"].get("live") else {}),
        "shadow_positions": len(state["books"]["schaduw"]["positions"]),
        "actions": {
            "buy": sum(1 for e in events if e.get("type") == "entry"),
            "sell": sum(1 for e in events if e.get("type") == "exit"),
            "blocked": sum(1 for e in events if e.get("type") == "blocked"),
        },
        "desks": {
            "monitor": True,
            "discovery": bool(new_discovery),
            "fast": bool(new_fast),
            "swing": bool(new_hour),
            "trend": bool(h4),
        },
        "errors": len(errors),
    })
    state["scan_history"] = scan_history[-2016:]

    monitor.record(state, now_ts, prices, prices.get(cfg["benchmark_market"]))
    desk_status = {
        "monitor_minutes": int(cadence.get("monitor_minutes", 5)),
        "discovery_minutes": discovery_tf,
        "fast_minutes": fast_tf,
        "swing_minutes": swing_tf,
        "trend_minutes": trend_tf,
        "fast_ran": bool(new_fast),
        "swing_ran": bool(new_hour),
        "trend_ran": bool(h4),
    }
    monitor.save(state, cfg, profile, prices, learner.summary(strategies),
                 {"markets": markets, "spark": state.get("spark", {}), "scanner": scan_report,
                  "cadence": desk_status})

    for line in state["narrative"]:
        print(line)
    for e in errors:
        print("  LET OP:", e)
    return state


if __name__ == "__main__":
    run()
    sys.exit(0)
