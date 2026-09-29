"""Data-agent: haalt via ccxt de markten, prijzen en candles op bij Kraken.

- Kiest elke run zelf de munten: de top N EUR-paren op 24-uursvolume (stablecoins uitgesloten),
  plus vaste munten en alles waar een positie in staat.
- 15-minuten- en uurcandles voor elke munt, elke run.
- 4-uurscandles alleen als er sinds de vorige run een 4-uurscandle is gesloten.
- Alleen afgesloten candles worden gebruikt.
"""
import time

TF = {15: "15m", 60: "1h", 240: "4h"}


class DataAgent:
    label = "Data-agent"

    def __init__(self, exchange, cfg):
        self.ex = exchange
        self.cfg = cfg

    def universe(self, held):
        markets = self.ex.load_markets()
        tickers = self.ex.fetch_tickers()
        excl = set(self.cfg["universe"]["exclude_bases"])
        ranked = []
        for sym, m in markets.items():
            if m.get("quote") != "EUR" or m.get("base") in excl or not m.get("active", True):
                continue
            if m.get("type") and m["type"] != "spot":
                continue
            t = tickers.get(sym) or {}
            vol = t.get("quoteVolume") or 0
            if vol > 0 and t.get("last"):
                ranked.append((vol, sym))
        ranked.sort(reverse=True)
        chosen = [s for _, s in ranked[:self.cfg["universe"]["size"]]]
        for s in list(self.cfg["universe"]["always_include"]) + list(held):
            if s in markets and s not in chosen:
                chosen.append(s)
        prices = {s: float(tickers[s]["last"]) for s in chosen if tickers.get(s, {}).get("last")}
        volumes = {s: float((tickers.get(s) or {}).get("quoteVolume") or 0) for s in chosen}
        return chosen, prices, volumes

    def candles(self, symbol, tf_minutes, now_ts, limit=720):
        raw = self.ex.fetch_ohlcv(symbol, timeframe=TF[tf_minutes], limit=limit)
        out = [{"ts": int(r[0] // 1000), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5] or 0)} for r in raw]
        return [c for c in out if c["ts"] + tf_minutes * 60 <= now_ts]  # alleen afgesloten candles

    @staticmethod
    def is_fresh(candles, tf, now_ts):
        return bool(candles) and now_ts - (candles[-1]["ts"] + tf * 60) < 3 * tf * 60

    @staticmethod
    def new_4h_close(last_scan_ts, now_ts):
        return last_scan_ts is None or now_ts // 14400 > last_scan_ts // 14400
