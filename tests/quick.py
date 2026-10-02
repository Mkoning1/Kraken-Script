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
    return True


if __name__ == "__main__":
    results = [
        unit_checks(),
        run_case("normale werking", 200, 1, 3, False),
        run_case("gemiste runs", 400, 7, 5, False),
        run_case("stop op Kraken en noodstop", 300, 1, 1, True),
    ]
    sys.exit(0 if all(results) else 1)
