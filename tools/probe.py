"""Onderzoek: wat kan dit Kraken-account verhandelen? Alleen lezen en 'validate' (Kraken keurt een order goed of af
zonder hem uit te voeren). Er wordt nooit een echte order geplaatst en er worden geen saldi bewaard."""
import json
import os
import re
import time
from pathlib import Path

import ccxt

ROOT = Path(__file__).resolve().parent.parent
ex = ccxt.kraken({"apiKey": os.environ.get("KRAKEN_API_KEY", ""), "secret": os.environ.get("KRAKEN_API_SECRET", ""),
                  "enableRateLimit": True})
out = {"ts": int(time.time())}


def call(name, params=None):
    try:
        return True, getattr(ex, name)(params or {})
    except Exception as e:
        return False, f"{e.__class__.__name__}: {str(e)[:350]}"


def validate_order(pair, side, volume, extra=None):
    params = {"pair": pair, "type": side, "ordertype": "market", "volume": str(volume), "validate": "true"}
    params.update(extra or {})
    assert params["validate"] == "true"  # nooit een echte order
    ok, res = call("privatePostAddOrder", params)
    answer = (res.get("result", {}).get("descr", {}).get("order") if ok and isinstance(res, dict) else res)
    return {"ok": ok, "pair": pair, "volume": volume, "extra": extra or {}, "answer": answer}


def eur_alts(pairs, needle):
    return sorted(p["altname"] for p in pairs.values() if needle in p.get("altname", "") and str(p.get("quote", "")).endswith("EUR"))


ok, res = call("publicGetAssetPairs")
pairs = res["result"] if ok else {}
out["default_list"] = {"ok": ok, "pairs": len(pairs), "eur_pairs": sum(1 for p in pairs.values() if str(p.get("quote", "")).endswith("EUR")),
                       "aclass_base": sorted({str(p.get("aclass_base", "?")) for p in pairs.values()}),
                       "error": None if ok else res}
xs = [p["altname"] for p in pairs.values() if re.fullmatch(r"[A-Z0-9]{1,8}x(USD|EUR)", p.get("altname", ""))]
out["xstocks_in_default_list"] = {"count": len(xs), "eur": [a for a in xs if a.endswith("EUR")][:50], "usd_sample": [a for a in xs if a.endswith("USD")][:20]}
out["gold_and_stable_pairs"] = {k: eur_alts(pairs, k) for k in ("PAXG", "XAUT", "USDC", "USDT", "EURC", "EURQ")}
for sym in ("XBTEUR", "ETHEUR"):
    p = next((p for p in pairs.values() if p.get("altname") == sym), {})
    out[f"margin_{sym}"] = {"leverage_buy": p.get("leverage_buy"), "leverage_sell": p.get("leverage_sell"), "ordermin": p.get("ordermin")}

# getokeniseerde aandelen (xStocks): aparte lijst via aclass_base
from collections import Counter
ok, res = call("publicGetAssetPairs", {"aclass_base": "tokenized_asset"})
xs_pairs = {}
if ok:
    xs_pairs = res["result"]
    alts = sorted({p["altname"] for p in xs_pairs.values()})
    out["tokenized"] = {"ok": True, "entries": len(xs_pairs), "unique_pairs": len(alts),
                        "quotes": dict(Counter(str(p.get("quote")) for p in xs_pairs.values())),
                        "eur_pairs": [a for a in alts if a.endswith("EUR")][:50], "pairs": alts}
else:
    out["tokenized"] = {"ok": False, "error": res}

# andere klassen? Een ongeldige waarde geeft meestal een foutmelding met de toegestane waarden.
classes = {}
for val in ("equity", "stock", "etf", "commodity", "fx", "currency"):
    ok2, r2 = call("publicGetAssetPairs", {"aclass_base": val})
    classes[val] = {"ok": ok2, "count": len(r2["result"]) if ok2 else None, "error": None if ok2 else r2}
out["other_asset_classes"] = classes

# controle: een gewone munt moet goed gekeurd worden
out["validate_control_btc"] = validate_order("XBTEUR", "buy", 0.0001)
# hefboom (margin) voor dit account? Alleen validate.
out["validate_margin_2x"] = validate_order("XBTEUR", "buy", 0.0001, {"leverage": "2"})

# een getokeniseerd aandeel valideren (nooit uitgevoerd)
target = None
for pref in ("AAPLxUSD", "TSLAxUSD", "NVDAxUSD", "MSFTxUSD", "SPYxUSD"):
    target = next((p for p in xs_pairs.values() if p.get("altname") == pref), None)
    if target:
        break
target = target or next(iter(xs_pairs.values()), None)
if target:
    out["stock_pair_info"] = {k: target.get(k) for k in ("altname", "wsname", "quote", "aclass_base", "aclass_quote", "ordermin", "costmin", "lot_decimals", "status", "fees")}
    om = float(target.get("ordermin") or 0.001)
    attempts = []
    for vol in (om * 10, 1):
        for extra in ({"asset_class": "tokenized_asset"}, {}):
            r = validate_order(target["altname"], "buy", round(vol, 8), extra)
            attempts.append(r)
            if r["ok"]:
                break
        if attempts[-1]["ok"]:
            break
    out["validate_stock"] = attempts
else:
    out["validate_stock"] = "geen getokeniseerd aandeel gevonden"

(ROOT / "data").mkdir(exist_ok=True)
(ROOT / "data" / "probe.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
print(json.dumps({k: v for k, v in out.items() if k in ("default_list", "stock_pair_info", "validate_control_btc", "validate_margin_2x")}, indent=1)[:3000])
