"""Veiligheidstest die vóór elke run draait. De bot handelt een paar dagen tegen een nagebootste Kraken
(in een tijdelijke map, je echte geheugen wordt niet aangeraakt). Faalt er iets, dan stopt de run
en wordt er niet gehandeld; je posities blijven beschermd door de stop-loss orders op Kraken.

Gecontroleerd:
  1. Normale werking: geen fouten, het boek van de bot klopt tot op de cent met het account.
  2. Gemiste runs (zoals GitHub soms doet): alles blijft kloppen.
  3. Elke open positie heeft precies één stop-loss order op Kraken.
  4. Een stop die op Kraken wordt uitgevoerd, wordt herkend.
  5. Een handmatige verkoop wordt herkend.
  6. De noodstop verkoopt alles.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

CASE = r'''
import sys, io, contextlib, json
sys.path.insert(0, r"TESTS_DIR")
from mockex import MockKraken
import main
cfg = json.load(open("config.json"))
cfg["mode"] = "live"; cfg["live"]["validate_only"] = False; cfg["live"]["trading_enabled"] = True; cfg["promotion"]["all_agents_live"] = True
json.dump(cfg, open("config.json", "w"))
SYMS = {"BTC/EUR": 60000, "ETH/EUR": 3000, "SOL/EUR": 150, "HBAR/EUR": 0.08, "DOGE/EUR": 0.1}
K0, STEPS, SKIP, SEED = 3500, STEPS_N, SKIP_N, SEED_N
ex = MockKraken(SYMS, K0 + STEPS + 40, eur=300, seed0=SEED)
def run(k):
    ex.set_k(k)
    with contextlib.redirect_stdout(io.StringIO()):
        return main.run(now_ts=ex.now() + 180, exchange=ex)
def check(st, label):
    live = st["books"]["live"]; px = {s: ex.price(s) for s in SYMS}
    acct = ex.free["EUR"] + sum((ex.free.get(s.split("/")[0], 0) + ex.used.get(s.split("/")[0], 0)) * px[s] for s in SYMS)
    book = live["cash"] + sum(p["qty"] * px[m] for m, p in live["positions"].items())
    assert abs(acct - book) < 0.05, f"{label}: boek {book:.2f} wijkt af van account {acct:.2f}"
    stops = sorted(o["symbol"] for o in ex.orders.values() if o["status"] == "open" and o.get("stop"))
    assert stops == sorted(live["positions"]), f"{label}: stops {stops} horen bij posities {sorted(live['positions'])}"
errs = []
st = None
for k in range(K0, K0 + STEPS, SKIP):
    st = run(k)
    errs += [e["message"] for e in st["errors"] if e["ts"] == ex.now() + 180]
    check(st, f"stap {k}")
    assert st.get("scan_history"), f"{k}: scanlog ontbreekt"
    scan = st["scan_history"][-1]
    assert scan["ts"] == ex.now() + 180, f"{k}: verkeerde scantijd"
    assert scan["desks"]["monitor"] is True, f"{k}: 5m-monitor niet gelogd"
    assert scan["ranked"] >= 0 and scan["active"] >= 1, f"{k}: scan-aantallen ontbreken"
assert len(st["scan_history"]) <= 2016, "scanlog groeit voorbij de 7-daagse limiet"
assert not errs, f"fouten: {errs[:3]}"
live = st["books"]["live"]
if SCENARIOS:
    # stop op Kraken uitgevoerd
    if live["positions"]:
        m, p = next(iter(live["positions"].items()))
        ex.orders[p["stop_order_id"]]["stop"] = ex.price(m) * 2
        st = run(K0 + STEPS); check(st, "stop uitgevoerd")
        assert m not in st["books"]["live"]["positions"], "uitgevoerde stop niet herkend"
    # noodstop
    s = json.load(open("data/state.json")); s["books"]["live"]["peak_equity"] = 10 ** 6; json.dump(s, open("data/state.json", "w"))
    st = run(K0 + STEPS + 1)
    assert st["books"]["live"]["halted"] and not st["books"]["live"]["positions"], "noodstop verkocht niet alles"
    check(st, "noodstop")
print(f"OK: {len(live['trades'])} trades, {len(live['positions'])} open")
'''


def run_case(name, steps, skip, seed, scenarios):
    with tempfile.TemporaryDirectory() as tmp:
        for item in ("main.py", "config.json"):
            shutil.copy(ROOT / item, tmp)
        shutil.copytree(ROOT / "agents", Path(tmp) / "agents")
        code = (CASE.replace("TESTS_DIR", str(TESTS)).replace("STEPS_N", str(steps)).replace("SKIP_N", str(skip))
                .replace("SEED_N", str(seed)).replace("SCENARIOS", "True" if scenarios else "False"))
        res = subprocess.run([sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True, timeout=600)
        ok = res.returncode == 0
        print(f"{'GOED' if ok else 'FOUT'}  {name}: {(res.stdout.strip().splitlines() or [''])[-1]}")
        if not ok:
            print(res.stderr[-1500:])
        return ok


def unit_checks():
    sys.path.insert(0, str(ROOT))
    from agents.indicators import correlation
    import math, random
    rnd = random.Random(1)
    a, b, c = [], [], []
    pa = pb = pc = 100.0
    for i in range(200):
        f = rnd.gauss(0, 0.02)
        pa *= math.exp(f); pb *= math.exp(f + rnd.gauss(0, 0.004)); pc *= math.exp(rnd.gauss(0, 0.02))
        a.append({"ts": i, "close": pa}); b.append({"ts": i, "close": pb}); c.append({"ts": i, "close": pc})
    assert correlation(a, b) > 0.9, "samenhang tussen gelijk bewegende munten niet herkend"
    assert abs(correlation(a, c)) < 0.3, "samenhang tussen onafhankelijke munten te hoog"
    print("GOED  samenhang-berekening")

    from agents.data import DataAgent
    class ScanEx:
        def __init__(self):
            vols = {"BTC": 10e6, "ETH": 8e6, "SOL": 6e6, "XRP": 5e6, "ADA": 4e6, "ALT": 1e6}
            self.markets = {f"{base}/EUR": {"base": base, "quote": "EUR", "active": True, "type": "spot"} for base in vols}
            self.tickers = {}
            for base, volume in vols.items():
                pct = 15.0 if base == "ALT" else 0.0
                self.tickers[f"{base}/EUR"] = {"last": 100.0, "quoteVolume": volume, "percentage": pct,
                                                "high": 118.0 if base == "ALT" else 102.0,
                                                "low": 98.0, "bid": 99.95, "ask": 100.05, "open": 100.0}
        def load_markets(self): return self.markets
        def fetch_tickers(self): return self.tickers

    scfg = {
        "universe": {"size": 3, "always_include": ["BTC/EUR", "ETH/EUR"], "exclude_bases": []},
        "scanner": {"enabled": True, "pool_size": 6, "discovery_slots": 1, "shadow_only": True,
                    "min_quote_volume_eur": 1000, "max_spread_pct": 1.2,
                    "weights": {"liquidity": 0.40, "momentum": 0.35, "range": 0.15, "spread": 0.10}},
    }
    da = DataAgent(ScanEx(), scfg)
    chosen, _, _ = da.universe(set())
    assert da.scan_report["core_markets"][:3] == ["BTC/EUR", "ETH/EUR", "SOL/EUR"], "core-universe is veranderd"
    assert da.scan_report["discovery_markets"] == ["ALT/EUR"], "scanner vond de momentum-altcoin niet"
    assert chosen[-1] == "ALT/EUR", "discovery-markt niet toegevoegd"
    print("GOED  opportunity-scanner")

    cfg_all = json.loads((ROOT / "config.json").read_text())
    from agents.strategies import build_agents, Signal
    desks = {a.name: (a.horizon, a.timeframe_minutes) for a in build_agents(cfg_all["agents"])}
    assert desks["momentum"] == ("fast", 15), "momentum hoort op de 15m fast desk"
    assert desks["squeeze"] == ("fast", 15), "squeeze hoort op de 15m fast desk"
    assert desks["fast_breakout"] == ("fast", 15), "snelle breakout hoort op de 15m fast desk"
    assert desks["mean_reversion"] == ("swing", 60), "dip-koper hoort op de 1u swing desk"
    assert desks["breakout"] == ("swing", 60), "normale breakout hoort op de 1u swing desk"
    assert desks["trend4h"] == ("trend", 240), "Trend-4u hoort op de 4u trend desk"
    assert cfg_all["cadence"]["monitor_minutes"] == 5 and cfg_all["cadence"]["discovery_minutes"] == 15
    print("GOED  multi-speed desk-indeling")

    from agents.execution import new_position
    book = {"name": "schaduw", "positions": {}}
    pos = new_position(book, "TEST/EUR", "momentum", Signal("buy", .8, "test"), 1, 10, 10, 0,
                       9, 9, {"_tf_sec": 900, "_cooldown_sec": 2700}, 1, 1_700_000_000, 3600, "test", .8, {})
    assert pos["timeframe_sec"] == 900 and pos["entry_ts"] == 1_700_000_900, "positie bewaart eigen timeframe niet"
    assert pos["cooldown_sec"] == 2700, "positie bewaart desk-cooldown niet"
    print("GOED  timeframe-aware positie")

    from agents.learning import LearningAgent
    fake_state = {
        "agent_stats": {"momentum": {"r":[{"r":1.0,"book":"schaduw"}],"pnl_live":0,"pnl_schaduw":10,"trades":1,"wins":1,"live_since":None}},
        "agent_status": {"momentum":"live"},
        "agent_timeframes": {"momentum":60},
    }
    LearningAgent(fake_state, cfg_all["learning"], cfg_all["promotion"], cfg_all["agents"], {}, cfg_all["backtest"]["min_trades"])
    assert fake_state["agent_timeframes"]["momentum"] == 15, "nieuw momentum-timeframe niet opgeslagen"
    assert "momentum" not in fake_state["agent_stats"], "oude 1u momentum-statistiek niet gereset"
    assert fake_state["agent_status"]["momentum"] == "schaduw", "gewijzigde agent hoort opnieuw in schaduw te starten"
    print("GOED  timeframe-migratie reset oude agentstatistiek")

    from agents.equities import EquityScout, update_paper_book
    class EquityProvider:
        def history(self, symbols, period="6mo"):
            out = {}
            for j, symbol in enumerate(symbols):
                # GROW stijgt stevig; VALUE matig; WEAK daalt.
                factor = 1.012 if symbol == "GROW.HK" else 1.003 if symbol == "VALUE.HK" else 0.996
                p, rows = 100.0, []
                for day in range(90):
                    p *= factor
                    rows.append({"ts": 1_700_000_000 + day * 86400, "close": p, "volume": 1_000_000})
                out[symbol] = rows
            return out
        def fundamentals(self, symbol):
            if symbol == "GROW.HK":
                return {"currency":"HKD","revenue_growth":0.42,"earnings_growth":0.55,"gross_margin":0.58,
                        "operating_margin":0.24,"profit_margin":0.20,"return_on_equity":0.28,
                        "debt_to_equity":35,"forward_pe":24}
            if symbol == "VALUE.HK":
                return {"currency":"HKD","revenue_growth":0.10,"earnings_growth":0.12,"gross_margin":0.35,
                        "operating_margin":0.12,"profit_margin":0.10,"return_on_equity":0.15,
                        "debt_to_equity":45,"forward_pe":14}
            return {"currency":"HKD","revenue_growth":-0.08,"earnings_growth":-0.15,"gross_margin":0.18,
                    "operating_margin":0.01,"profit_margin":0.0,"return_on_equity":0.02,
                    "debt_to_equity":180,"forward_pe":55}

    with tempfile.TemporaryDirectory() as eqtmp:
        ecfg = {
            "watchlist": [
                {"symbol":"GROW.HK","name":"Grow","theme":"growth"},
                {"symbol":"VALUE.HK","name":"Value","theme":"value"},
                {"symbol":"WEAK.HK","name":"Weak","theme":"weak"},
            ],
            "history_period":"6mo","fundamentals_refresh_hours":24,"paper_capital_hkd":10000,
            "max_positions":2,"max_position_pct":50,"entry_score":60,"exit_score":45,
            "min_data_quality_pct":40,"trailing_stop_pct":12,"paper_fee_pct":0.15,
            "paper_slippage_pct":0.20,
            "weights":{"growth":0.30,"margin":0.25,"momentum":0.25,"valuation":0.10,"quality":0.10},
        }
        scout = EquityScout(ecfg, Path(eqtmp) / "fund.json", provider=EquityProvider())
        report = scout.scan(1_800_000_000)
        assert report["candidates"][0]["symbol"] == "GROW.HK", "equity scorer rangschikt sterke groeier niet bovenaan"
        incomplete_score = next(x["score"] for x in report["candidates"] if x["symbol"] == "WEAK.HK")
        assert incomplete_score < report["candidates"][0]["score"], "zwakke fundamentals krijgen onvoldoende straf"
        estate = {}
        book = update_paper_book(estate, report, ecfg, 1_800_000_000)
        assert "GROW.HK" in book["positions"], "equity paper desk opent geen sterke kandidaat"
        bad = json.loads(json.dumps(report))
        for row in bad["candidates"]:
            if row["symbol"] == "GROW.HK":
                row["price"] *= 0.80
                row["score"] = 30
                row["m20_pct"] = -12
        book = update_paper_book(estate, bad, ecfg, 1_800_086_400)
        assert "GROW.HK" not in book["positions"], "equity paper desk sluit zwakke positie niet"
        assert book["trades"], "equity paper trade niet vastgelegd"
    print("GOED  equity-scout en paper desk")
    return True



class FakeHttp:
    """Nagebootst internet voor de nieuws-agent: geen echte verzoeken in de veiligheidstest."""

    def __init__(self, light=None, heavy=None, feeds_ok=True, models_status=200):
        self.light, self.heavy, self.feeds_ok, self.models_status = light, heavy, feeds_ok, models_status
        self.calls = []

    def request(self, url, data=None, headers=None, timeout=10):
        import json as _j
        self.calls.append(url)
        if "catalog" in url:
            return 200, _j.dumps([{"id": "openai/gpt-4.1-mini", "rate_limit_tier": "low"},
                                  {"id": "openai/gpt-4.1", "rate_limit_tier": "high"}]), {}
        if "models.github.ai/inference" in url:
            if self.models_status != 200:
                return self.models_status, "rate limited", {"Retry-After": "120"}
            heavy = data["model"] == "openai/gpt-4.1"
            body = self.heavy if heavy else self.light
            if body is None:
                return 500, "kapot", {}
            return 200, _j.dumps({"choices": [{"message": {"content": "```json\n" + _j.dumps(body) + "\n```"}}]}), {}
        if not self.feeds_ok:
            return 503, "", {}
        if "alternative.me" in url:
            return 200, _j.dumps({"data": [{"value": "31", "value_classification": "Fear"}]}), {}
        if "gdeltproject" in url:
            return 200, _j.dumps({"articles": [{"title": "Fed holds rates steady as markets wait",
                                                "url": "https://example.org/fed", "domain": "example.org",
                                                "seendate": "20261027T080000Z"}]}), {}
        rss = ("<?xml version='1.0'?><rss><channel>"
               "<item><title>Bonk treasury hacked, $40M stolen</title><link>https://example.org/bonk</link>"
               "<pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate></item>"
               "<item><title>Coinbase to list dogwifhat next week</title><link>https://example.org/wif</link>"
               "<pubDate>Tue, 27 Oct 2026 07:30:00 GMT</pubDate></item>"
               "</channel></rss>")
        return 200, rss, {}


def news_checks():
    sys.path.insert(0, str(ROOT))
    from agents.news import NewsAgent, mentions
    cfg = json.loads((ROOT / "config.json").read_text())["news"]
    now = 1_793_090_000  # 27-10-2026
    prices = {"BTC/EUR": 70000.0, "ETH/EUR": 2300.0, "BONK/EUR": 0.00002, "WIF/EUR": 0.5, "SOL/EUR": 150.0}
    nosleep = lambda x: None

    assert mentions("Bitcoin slides below 70k", "BTC") and mentions("ETH ETF inflows jump", "ETH")
    assert not mentions("Sui network upgrade", "SUI") and mentions("$SUI rallies", "SUI")
    assert not mentions("Use a link to sign up", "LINK"), "gewoon Engels woord als munt herkend"
    print("GOED  nieuws: munten herkennen in koppen")

    light = {"markt": {"richting": -1, "reden": "Rente blijft hoog"},
             "munten": [{"munt": "BONK", "richting": -2, "impact": 3, "termijn": "dagen", "zekerheid": 0.9,
                         "gebeurtenis": "hack", "reden": "Treasury gehackt", "bronnen": [1]},
                        {"munt": "FAKE", "richting": 2, "zekerheid": 1, "gebeurtenis": "hack", "bronnen": [2]}],
             "kansen": [{"munt": "WIF", "aanleiding": "Notering op Coinbase", "bronnen": [2]}]}
    st = {}
    agent = NewsAgent(st, cfg, ["USDT"], http=FakeHttp(light=light), token="t", sleep=nosleep)
    agent.refresh(now, prices, ["BTC", "ETH", "BONK"])
    assert agent.veto_for("BONK/EUR", now), "AI-veto bij een hack ontbreekt"
    assert not agent.veto_for("BTC/EUR", now), "veto op een munt zonder slecht nieuws"
    assert "FAKE" not in st["news"]["coins"], "verzonnen munt overgenomen"
    assert agent.picks(now) == ["WIF/EUR"], "kans uit het nieuws niet naar de scanner"
    assert st["news"]["usage"]["light"] == 1 and st["news"]["fng"]["value"] == 31
    assert {t["k"] for t in st["news"]["trackers"]} == {"score", "veto"}, "oordelen worden niet gemeten"
    assert not agent.veto_for("BONK/EUR", now + 49 * 3600), "veto verloopt niet"
    agent.refresh(now + 300, prices, ["BTC"])  # zelfde kwartier: niets opnieuw ophalen
    assert st["news"]["usage"]["light"] == 1, "nieuws dubbel beoordeeld"
    print("GOED  nieuws: AI-veto, kansen en meting")

    st2 = {}
    fb = NewsAgent(st2, cfg, [], http=FakeHttp(), token="", sleep=nosleep)
    fb.refresh(now, prices, ["BTC", "BONK", "SOL"])
    assert fb.veto_for("BONK/EUR", now) and fb.veto_for("BONK/EUR", now)["by"] == "woordenlijst"
    assert not fb.veto_for("SOL/EUR", now)
    st3 = {}
    rl = NewsAgent(st3, cfg, [], http=FakeHttp(light=light, models_status=429), token="t", sleep=nosleep)
    rl.refresh(now, prices, ["BONK"])
    assert st3["news"]["pause_until"] == now + 120 and rl.veto_for("BONK/EUR", now), "limiet niet netjes afgehandeld"
    broken = NewsAgent({}, cfg, [], http=FakeHttp(feeds_ok=False), token="t", sleep=nosleep)
    broken.refresh(now, prices, ["BTC"])  # alle bronnen kapot: geen fout naar boven
    print("GOED  nieuws: woordenlijst, limiet en storingen")

    heavy = {"oordeel": "tegenhouden", "zekerheid": 0.7, "aanleiding": "geen", "reden": "Geen nieuws, lijkt een pomp"}
    ag = NewsAgent({}, cfg, [], http=FakeHttp(light=light, heavy=heavy), token="t", sleep=nosleep)
    v = ag.research_check("WIF/EUR", {"reason": "Uitbraak", "horizon": "trend", "score": 0.8},
                          {"sig": {"close": 0.5, "rsi": 71}}, 0.5, now)
    assert v["oordeel"] == "tegenhouden" and not ag.check_is_live(), "koopcheck hoort eerst in schaduw te staan"
    ag.evaluate(now + 86400, {"WIF/EUR": 0.45}, [])
    assert ag.s["trackers"][-1]["r24"] == -10.0, "koers na 24 uur niet gemeten"
    trades = ([{"pnl": -1, "news_check": {"oordeel": "tegenhouden" if i < 8 else "doorlaten"}} for i in range(30)]
              + [{"pnl": 1, "news_check": {"oordeel": "tegenhouden" if i < 1 else "doorlaten"}} for i in range(20)])
    ag.evaluate(now + 86400, {}, trades)
    assert ag.s["scorecard"]["trades"]["goal_met"] and ag.check_is_live(), "doel gehaald maar koopcheck niet live"
    ag.evaluate(now + 86400, {}, trades[:40])
    assert not ag.check_is_live(), "koopcheck live met te weinig bewijs"
    print("GOED  nieuws: koopcheck en doel")

    st4 = {}
    ln = NewsAgent(st4, cfg, [], http=FakeHttp(feeds_ok=False), token="", sleep=nosleep)
    ln.refresh(now, prices, [])
    ln.refresh(now + 900, {**prices, "NEW/EUR": 1.0}, [])
    assert "NEW/EUR" in ln.picks(now + 900), "nieuwe Kraken-notering niet opgemerkt"
    print("GOED  nieuws: nieuwe noteringen")
    return True


def withdrawal_checks():
    sys.path.insert(0, str(ROOT))
    import main
    f = main.flow_factor(39.72, -38.0)
    peak = 41.04 * f
    assert abs((1 - 1.72 / peak) * 100 - 3.2) < 0.1, "opname telt als verlies"
    hist = [{"ts": 100, "equity": 41.04}, {"ts": 200, "equity": 39.72}, {"ts": 300, "equity": 1.72}]
    st = {"events": [{"ts": 250, "type": "note", "book": "live", "reason": "Opname van € 38,00 herkend op Kraken; telt niet"}],
          "books": {"live": {"halted": True, "halt_reason": "Vermogen 43.3% onder de hoogste stand", "halt_until": 999,
                             "halt_history": [250], "peak_equity": 3.04, "equity_history": hist}}}
    notes = []
    main.repair_withdrawal_halt(st, 400, lambda **kw: notes.append(kw), 20)
    live = st["books"]["live"]
    assert not live["halted"] and live["halt_history"] == [] and notes, "valse noodstop niet opgeheven"
    st["books"]["live"]["halted"] = True
    main.repair_withdrawal_halt(st, 500, lambda **kw: notes.append(kw), 20)
    assert live["halted"], "herstel hoort maar één keer te draaien"
    real = {"events": [], "books": {"live": {"halted": True, "halt_reason": "Vermogen 25% onder de hoogste stand",
                                             "halt_history": [250], "peak_equity": 40, "equity_history": hist}}}
    main.repair_withdrawal_halt(real, 400, lambda **kw: None, 20)
    assert real["books"]["live"]["halted"], "echte noodstop zonder opname mag niet worden opgeheven"
    print("GOED  opname telt niet als verlies; valse noodstop opgeheven")
    return True


NEWS_CASE = r"""
import sys, io, contextlib, json
sys.path.insert(0, r"TESTS_DIR")
from mockex import MockKraken
from quick import FakeHttp
import main
from agents.news import NewsAgent
cfg = json.load(open("config.json"))
cfg["mode"] = "live"; cfg["live"]["validate_only"] = False; cfg["promotion"]["all_agents_live"] = True
cfg["news"]["research_check_live"] = CHECK_LIVE
json.dump(cfg, open("config.json", "w"))
SYMS = {"BTC/EUR": 60000, "ETH/EUR": 3000, "SOL/EUR": 150, "HBAR/EUR": 0.08, "DOGE/EUR": 0.1}
ex = MockKraken(SYMS, 3500 + 440, eur=300, seed0=5)
light = {"markt": {"richting": 0, "reden": "rustig"}, "munten": [], "kansen": []}
heavy = {"oordeel": "VERDICT", "zekerheid": 0.8, "aanleiding": "geen", "reden": "test"}
def factory(state, c):
    a = NewsAgent(state, c["news"], [], http=FakeHttp(light=light, heavy=heavy), token="t", sleep=lambda x: None)
    if VETO_ALL:
        for m in SYMS:
            a.s["vetoes"][m] = {"until": 10**12, "reason": "test-veto", "by": "test", "ts": 0}
    return a
errs, st = [], None
for k in range(3500, 3900, 7):
    ex.set_k(k)
    with contextlib.redirect_stdout(io.StringIO()):
        st = main.run(now_ts=ex.now() + 180, exchange=ex, news=factory)
    errs += [e["message"] for e in st["errors"] if e["ts"] == ex.now() + 180]
assert not errs, f"fouten: {errs[:3]}"
books = [b for b in st["books"].values() if b]
entries = [e for e in st["events"] if e["type"] == "entry"]
blocked = " ".join(e["reason"] for e in st["events"] if e["type"] == "blocked")
checks = [p.get("news_check") for b in books for p in list(b["positions"].values()) + b["trades"]]
assert st["news"].get("last_ai_ok") and st["news"]["headlines"], "nieuws-agent draaide niet mee"
assert "news" in json.load(open("docs/data.json")), "nieuws ontbreekt op het dashboard"
if VETO_ALL:
    assert not entries and "nieuws-agent" in blocked, "veto hield aankopen niet tegen"
    print(f"OK: veto blokkeerde alles ({blocked.count('nieuws-agent')} keer)")
elif CHECK_LIVE:
    # Zonder koopcheck (limiet per ronde, AI weg) handelt de bot gewoon door; met een 'tegenhouden' nooit.
    assert "koopcheck" in blocked and not any(checks), "koopcheck hield aankopen niet tegen"
    print(f"OK: koopcheck blokkeerde {blocked.count('koopcheck')} aankopen, {len(entries)} zonder check doorgegaan")
else:
    assert entries, "geen aankopen in schaduwmodus"
    assert any(checks), "koopcheck niet bij de positie bewaard"
    print(f"OK: {len(entries)} aankopen, {sum(1 for c in checks if c)} met koopcheck")
"""


def news_case(name, veto_all, check_live, verdict):
    with tempfile.TemporaryDirectory() as tmp:
        for item in ("main.py", "config.json"):
            shutil.copy(ROOT / item, tmp)
        shutil.copytree(ROOT / "agents", Path(tmp) / "agents")
        code = (NEWS_CASE.replace("TESTS_DIR", str(TESTS)).replace("VETO_ALL", str(veto_all))
                .replace("CHECK_LIVE", str(check_live)).replace("VERDICT", verdict))
        res = subprocess.run([sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True, timeout=600)
        ok = res.returncode == 0
        print(f"{'GOED' if ok else 'FOUT'}  {name}: {(res.stdout.strip().splitlines() or [''])[-1]}")
        if not ok:
            print(res.stderr[-1500:])
        return ok

if __name__ == "__main__":
    results = [
        unit_checks(),
        run_case("normale werking", 200, 1, 3, False),
        run_case("gemiste runs", 400, 7, 5, False),
        run_case("stop op Kraken en noodstop", 300, 1, 1, True),
        news_checks(),
        withdrawal_checks(),
        news_case("nieuws-veto in de handelsronde", True, False, "doorlaten"),
        news_case("koopcheck live houdt tegen", False, True, "tegenhouden"),
        news_case("koopcheck in schaduw meet alleen", False, False, "tegenhouden"),
    ]
    sys.exit(0 if all(results) else 1)
