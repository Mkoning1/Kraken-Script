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
cfg["mode"] = "live"; cfg["live"]["validate_only"] = False; cfg["promotion"]["all_agents_live"] = True
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


if __name__ == "__main__":
    results = [
        unit_checks(),
        run_case("normale werking", 200, 1, 3, False),
        run_case("gemiste runs", 400, 7, 5, False),
        run_case("stop op Kraken en noodstop", 300, 1, 1, True),
    ]
    sys.exit(0 if all(results) else 1)
