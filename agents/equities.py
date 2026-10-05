"""China/Hong Kong equity research desk.

Deze desk is bewust paper-only. Marktdata komt via Yahoo Finance/yfinance zonder broker credentials.
Fundamentals worden lokaal gecachet zodat de kwartierloop niet telkens dezelfde zware requests doet.
Live-aandelenexecutie hoort later in een aparte broker-adapter (bijv. IBKR), niet in de Kraken-client.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path


def clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))


def pct_change(new, old):
    return (new / old - 1) * 100 if old else 0.0


def _num(value):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class YahooEquityProvider:
    """Kleine adapter rond yfinance; import is lazy zodat unit tests geen netwerkpakket nodig hebben."""

    def history(self, symbols, period="6mo"):
        import yfinance as yf

        raw = yf.download(
            tickers=" ".join(symbols),
            period=period,
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
            group_by="ticker",
        )
        out = {}
        for symbol in symbols:
            try:
                frame = raw[symbol] if len(symbols) > 1 else raw
                rows = []
                for idx, row in frame.iterrows():
                    close = _num(row.get("Close"))
                    volume = _num(row.get("Volume")) or 0.0
                    if close and close > 0:
                        rows.append({"ts": int(idx.timestamp()), "close": close, "volume": volume})
                if rows:
                    out[symbol] = rows
            except Exception:
                continue
        return out

    def fundamentals(self, symbol):
        import yfinance as yf

        info = yf.Ticker(symbol).info or {}
        return {
            "currency": info.get("currency"),
            "market_cap": _num(info.get("marketCap")),
            "revenue_growth": _num(info.get("revenueGrowth")),
            "earnings_growth": _num(info.get("earningsGrowth")),
            "gross_margin": _num(info.get("grossMargins")),
            "operating_margin": _num(info.get("operatingMargins")),
            "profit_margin": _num(info.get("profitMargins")),
            "return_on_equity": _num(info.get("returnOnEquity")),
            "debt_to_equity": _num(info.get("debtToEquity")),
            "forward_pe": _num(info.get("forwardPE")),
            "trailing_pe": _num(info.get("trailingPE")),
        }


def _momentum(history):
    closes = [float(x["close"]) for x in history if x.get("close")]
    if len(closes) < 3:
        return {"last": closes[-1] if closes else None, "m5": None, "m20": None, "m60": None, "volatility": None}
    last = closes[-1]

    def ret(days):
        return pct_change(last, closes[-days - 1]) if len(closes) > days else None

    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    vol = statistics.pstdev(returns[-60:]) * math.sqrt(252) * 100 if len(returns) >= 10 else None
    return {"last": last, "m5": ret(5), "m20": ret(20), "m60": ret(60), "volatility": vol}


def _component_scores(f, mom):
    comps = {}

    growth_parts = []
    if f.get("revenue_growth") is not None:
        growth_parts.append(clamp((f["revenue_growth"] + 0.05) / 0.55))
    if f.get("earnings_growth") is not None:
        growth_parts.append(clamp((f["earnings_growth"] + 0.10) / 0.80))
    if growth_parts:
        comps["growth"] = sum(growth_parts) / len(growth_parts)

    margin_parts = []
    if f.get("gross_margin") is not None:
        margin_parts.append(clamp(f["gross_margin"] / 0.70))
    if f.get("operating_margin") is not None:
        margin_parts.append(clamp((f["operating_margin"] + 0.05) / 0.35))
    if f.get("profit_margin") is not None:
        margin_parts.append(clamp((f["profit_margin"] + 0.02) / 0.27))
    if margin_parts:
        comps["margin"] = sum(margin_parts) / len(margin_parts)

    momentum_parts = []
    if mom.get("m20") is not None:
        momentum_parts.append(clamp((mom["m20"] + 10) / 40))
    if mom.get("m60") is not None:
        momentum_parts.append(clamp((mom["m60"] + 15) / 60))
    elif mom.get("m5") is not None:
        momentum_parts.append(clamp((mom["m5"] + 5) / 20))
    if momentum_parts:
        comps["momentum"] = sum(momentum_parts) / len(momentum_parts)

    pe = f.get("forward_pe") if f.get("forward_pe") and f["forward_pe"] > 0 else f.get("trailing_pe")
    if pe and pe > 0:
        comps["valuation"] = 1.0 - clamp((pe - 10) / 50)

    quality_parts = []
    if f.get("return_on_equity") is not None:
        quality_parts.append(clamp((f["return_on_equity"] + 0.05) / 0.40))
    if f.get("debt_to_equity") is not None:
        quality_parts.append(1.0 - clamp(f["debt_to_equity"] / 200))
    if quality_parts:
        comps["quality"] = sum(quality_parts) / len(quality_parts)

    return comps


def score_equity(fundamentals, momentum, weights):
    comps = _component_scores(fundamentals, momentum)
    weighted = [(float(weights.get(k, 0)), v) for k, v in comps.items() if float(weights.get(k, 0)) > 0]
    total_w = sum(w for w, _ in weighted)
    score = sum(w * v for w, v in weighted) / total_w if total_w else 0.0
    requested = [k for k in ("growth", "margin", "momentum", "valuation", "quality") if float(weights.get(k, 0)) > 0]
    quality = len(comps) / max(1, len(requested)) * 100
    return round(score * 100, 1), {k: round(v * 100, 1) for k, v in comps.items()}, round(quality, 0)


class EquityScout:
    def __init__(self, cfg, cache_path, provider=None):
        self.cfg = cfg
        self.cache_path = Path(cache_path)
        self.provider = provider or YahooEquityProvider()

    def _load_cache(self):
        try:
            return json.loads(self.cache_path.read_text())
        except Exception:
            return {"updated_ts": 0, "symbols": {}}

    def _save_cache(self, cache):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(cache, separators=(",", ":"), ensure_ascii=False))

    def scan(self, now_ts=None):
        now_ts = int(now_ts or time.time())
        watch = self.cfg.get("watchlist", [])
        symbols = [x["symbol"] for x in watch]
        history = self.provider.history(symbols, self.cfg.get("history_period", "6mo"))
        cache = self._load_cache()
        refresh_after = int(self.cfg.get("fundamentals_refresh_hours", 24)) * 3600
        stale = now_ts - int(cache.get("updated_ts", 0)) >= refresh_after

        fundamentals = dict(cache.get("symbols", {}))
        if stale:
            refreshed = {}
            for item in watch:
                symbol = item["symbol"]
                try:
                    refreshed[symbol] = self.provider.fundamentals(symbol)
                except Exception:
                    refreshed[symbol] = fundamentals.get(symbol, {})
            fundamentals = refreshed
            cache = {"updated_ts": now_ts, "symbols": fundamentals}
            self._save_cache(cache)

        rows = []
        weights = self.cfg.get("weights", {})
        for meta in watch:
            symbol = meta["symbol"]
            hist = history.get(symbol, [])
            mom = _momentum(hist)
            if mom.get("last") is None:
                continue
            f = fundamentals.get(symbol, {})
            score, components, data_quality = score_equity(f, mom, weights)
            rows.append({
                **meta,
                "price": round(mom["last"], 4),
                "currency": f.get("currency") or "HKD",
                "m5_pct": None if mom["m5"] is None else round(mom["m5"], 2),
                "m20_pct": None if mom["m20"] is None else round(mom["m20"], 2),
                "m60_pct": None if mom["m60"] is None else round(mom["m60"], 2),
                "volatility_pct": None if mom["volatility"] is None else round(mom["volatility"], 1),
                "score": score,
                "components": components,
                "data_quality_pct": data_quality,
                "fundamentals": f,
            })

        rows.sort(key=lambda x: (x["score"], x.get("m20_pct") or -999), reverse=True)
        return {
            "enabled": True,
            "paper_only": True,
            "source": "Yahoo Finance via yfinance",
            "generated_ts": now_ts,
            "market": self.cfg.get("market", "Hong Kong"),
            "symbols_requested": len(symbols),
            "symbols_loaded": len(rows),
            "candidates": rows,
        }


def _equity_value(book, by_symbol):
    return book["cash"] + sum(p["qty"] * by_symbol.get(sym, {}).get("price", p["entry_price"])
                              for sym, p in book["positions"].items())


def update_paper_book(state, report, cfg, now_ts):
    capital = float(cfg.get("paper_capital_hkd", 10000))
    book = state.setdefault("equity_paper", {
        "currency": "HKD", "starting_capital": capital, "cash": capital,
        "positions": {}, "trades": [], "equity_history": [], "fees_paid": 0.0,
    })
    by_symbol = {r["symbol"]: r for r in report.get("candidates", [])}
    fee = float(cfg.get("paper_fee_pct", 0.15)) / 100
    slip = float(cfg.get("paper_slippage_pct", 0.20)) / 100
    trail = float(cfg.get("trailing_stop_pct", 12)) / 100

    for symbol in list(book["positions"]):
        p = book["positions"][symbol]
        row = by_symbol.get(symbol)
        if not row:
            continue
        price = row["price"]
        p["peak"] = max(p.get("peak", p["entry_price"]), price)
        p["last_price"] = price
        m20 = row.get("m20_pct")
        reason = None
        if price <= p["peak"] * (1 - trail):
            reason = f"Trailing stop {cfg.get('trailing_stop_pct', 12)}%"
        elif row["score"] < float(cfg.get("exit_score", 50)):
            reason = f"Opportunity score gedaald naar {row['score']}"
        elif m20 is not None and m20 < -5:
            reason = f"20-daags momentum negatief ({m20:+.1f}%)"
        if reason:
            fill = price * (1 - slip)
            proceeds = p["qty"] * fill
            exit_fee = proceeds * fee
            net = proceeds - exit_fee
            pnl = net - p["entry_cost"]
            book["cash"] += net
            book["fees_paid"] += exit_fee
            book["trades"].append({
                "symbol": symbol, "name": p["name"], "entry_ts": p["entry_ts"], "exit_ts": now_ts,
                "entry_price": p["entry_price"], "exit_price": round(fill, 4), "qty": p["qty"],
                "pnl": round(pnl, 2), "pnl_pct": round(pnl / p["entry_cost"] * 100, 2) if p["entry_cost"] else 0,
                "reason": reason,
            })
            book["positions"].pop(symbol, None)

    max_pos = int(cfg.get("max_positions", 3))
    entry_score = float(cfg.get("entry_score", 70))
    min_quality = float(cfg.get("min_data_quality_pct", 40))
    candidates = [
        r for r in report.get("candidates", [])
        if r["symbol"] not in book["positions"]
        and r["score"] >= entry_score
        and r["data_quality_pct"] >= min_quality
        and (r.get("m20_pct") or 0) > 0
    ]
    slots = max(0, max_pos - len(book["positions"]))
    for row in candidates[:slots]:
        if book["cash"] <= 0:
            break
        remaining_slots = max(1, max_pos - len(book["positions"]))
        alloc = min(book["cash"] / remaining_slots, _equity_value(book, by_symbol) * float(cfg.get("max_position_pct", 35)) / 100)
        fill = row["price"] * (1 + slip)
        qty = math.floor(alloc / (fill * (1 + fee)))
        if qty < 1:
            continue
        cost = qty * fill
        entry_fee = cost * fee
        total = cost + entry_fee
        if total > book["cash"]:
            continue
        book["cash"] -= total
        book["fees_paid"] += entry_fee
        book["positions"][row["symbol"]] = {
            "symbol": row["symbol"], "name": row["name"], "qty": qty, "entry_price": round(fill, 4),
            "entry_cost": total, "entry_ts": now_ts, "peak": row["price"], "last_price": row["price"],
            "entry_score": row["score"], "theme": row.get("theme"),
        }

    eq = _equity_value(book, by_symbol)
    book["equity_history"].append({"ts": now_ts, "equity": round(eq, 2)})
    book["equity_history"] = book["equity_history"][-720:]
    book["trades"] = book["trades"][-300:]
    return book


def paper_dashboard(book, report):
    by_symbol = {r["symbol"]: r for r in report.get("candidates", [])}
    positions = []
    for symbol, p in book.get("positions", {}).items():
        price = by_symbol.get(symbol, {}).get("price", p.get("last_price", p["entry_price"]))
        value = p["qty"] * price
        positions.append({
            **p, "price": price, "value": round(value, 2),
            "unrealized": round(value - p["entry_cost"], 2),
            "score": by_symbol.get(symbol, {}).get("score"),
        })
    equity = book.get("cash", 0) + sum(p["value"] for p in positions)
    start = book.get("starting_capital", 1) or 1
    trades = book.get("trades", [])
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    return {
        "currency": book.get("currency", "HKD"),
        "cash": round(book.get("cash", 0), 2),
        "equity": round(equity, 2),
        "start": round(start, 2),
        "return_pct": round((equity / start - 1) * 100, 2),
        "positions": positions,
        "trades": list(reversed(trades[-50:])),
        "trade_count": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "fees_paid": round(book.get("fees_paid", 0), 2),
        "equity_history": book.get("equity_history", []),
    }


def build_opportunity_board(crypto_scanner, equities, limit=10):
    board = []
    for row in (crypto_scanner or {}).get("candidates", []):
        board.append({
            "asset_class": "crypto", "symbol": row["market"], "name": row["market"].split("/")[0],
            "score": round(float(row.get("score", 0)) * 100, 1),
            "momentum_pct": row.get("change_pct"), "mode": "shadow" if row.get("selected") else "radar",
            "reason": "Kraken: liquiditeit + 24u momentum + range + spread",
        })
    for row in (equities or {}).get("candidates", []):
        board.append({
            "asset_class": "equity", "symbol": row["symbol"], "name": row["name"],
            "score": row["score"], "momentum_pct": row.get("m20_pct"), "mode": "paper",
            "reason": f"{row.get('theme', 'China/HK')}: groei + marges + momentum + waardering + kwaliteit",
        })
    board.sort(key=lambda x: x["score"], reverse=True)
    return board[:limit]
