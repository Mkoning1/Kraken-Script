"""Data-agent: snelle marktselectie en candledata voor Kraken.

De selectie heeft twee lagen:
- core: de grootste liquide EUR-spotmarkten (de bewezen live-universe);
- discovery: extra kansrijke altcoins uit een bredere liquiditeitspool.

De opportunity scanner gebruikt alleen de ticker-snapshot (één Kraken-call) om volume,
24u-momentum, dagrange en spread te rangschikken. Alleen geselecteerde markten krijgen
daarna de zwaardere OHLCV-analyse. Discovery-markten kunnen shadow-only blijven.
"""
TF = {15: "15m", 60: "1h", 240: "4h"}


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))


class DataAgent:
    label = "Data-agent"

    def __init__(self, exchange, cfg):
        self.ex = exchange
        self.cfg = cfg
        self.scan_report = {"enabled": False, "candidates": [], "core_markets": [], "discovery_markets": []}
        self.all_prices = {}

    @staticmethod
    def _change_pct(ticker, last):
        pct = ticker.get("percentage")
        if pct is not None:
            return _f(pct)
        open_px = _f(ticker.get("open"))
        return (last / open_px - 1) * 100 if open_px > 0 and last > 0 else 0.0

    @staticmethod
    def _range_pct(ticker, last):
        high, low = _f(ticker.get("high")), _f(ticker.get("low"))
        return (high - low) / last * 100 if last > 0 and high >= low > 0 else 0.0

    @staticmethod
    def _spread_pct(ticker, last):
        bid, ask = _f(ticker.get("bid")), _f(ticker.get("ask"))
        return (ask - bid) / last * 100 if last > 0 and ask >= bid > 0 else None

    def universe(self, held):
        markets = self.ex.load_markets()
        tickers = self.ex.fetch_tickers()
        ucfg = self.cfg["universe"]
        scfg = self.cfg.get("scanner", {})
        excl = set(ucfg["exclude_bases"])
        eligible = []

        for sym, m in markets.items():
            if m.get("quote") != "EUR" or m.get("base") in excl or not m.get("active", True):
                continue
            if m.get("type") and m["type"] != "spot":
                continue
            t = tickers.get(sym) or {}
            last = _f(t.get("last"))
            vol = _f(t.get("quoteVolume"))
            if last <= 0 or vol <= 0:
                continue
            self.all_prices[sym] = last
            spread = self._spread_pct(t, last)
            eligible.append({
                "market": sym,
                "quote_volume": vol,
                "change_pct": self._change_pct(t, last),
                "range_pct": self._range_pct(t, last),
                "spread_pct": spread,
            })

        eligible.sort(key=lambda x: x["quote_volume"], reverse=True)
        core = [r["market"] for r in eligible[:int(ucfg["size"])]]
        for sym in list(ucfg.get("always_include", [])):
            if sym in self.all_prices and sym not in core:
                core.append(sym)

        discovery = []
        candidates = []
        pool_count = 0
        if scfg.get("enabled", False):
            min_vol = float(scfg.get("min_quote_volume_eur", 50000))
            max_spread = float(scfg.get("max_spread_pct", 1.2))
            pool_size = max(int(scfg.get("pool_size", 60)), len(core))
            pool = [
                r for r in eligible
                if r["quote_volume"] >= min_vol and (r["spread_pct"] is None or r["spread_pct"] <= max_spread)
            ][:pool_size]
            pool_count = len(pool)
            weights = scfg.get("weights", {})
            w_liq = float(weights.get("liquidity", 0.40))
            w_mom = float(weights.get("momentum", 0.35))
            w_rng = float(weights.get("range", 0.15))
            w_spr = float(weights.get("spread", 0.10))
            denom = max(1, len(pool) - 1)

            scored = []
            for rank, row in enumerate(pool):
                liq = 1.0 - rank / denom
                mom = _clamp(max(0.0, row["change_pct"]) / 15.0)
                rng = _clamp(row["range_pct"] / 20.0)
                spr = 0.5 if row["spread_pct"] is None else 1.0 - _clamp(row["spread_pct"] / max(max_spread, 0.01))
                score = (w_liq * liq + w_mom * mom + w_rng * rng + w_spr * spr) / max(w_liq + w_mom + w_rng + w_spr, 1e-9)
                scored.append({**row, "score": round(score, 4)})

            scored.sort(key=lambda x: (x["score"], x["quote_volume"]), reverse=True)
            blocked = set(core) | set(held)
            slots = max(0, int(scfg.get("discovery_slots", 4)))
            discovery = [r["market"] for r in scored if r["market"] not in blocked][:slots]
            selected = set(discovery)
            candidates = [{**r, "selected": r["market"] in selected} for r in scored[:12]]

        chosen = list(dict.fromkeys(core + discovery + list(held)))
        prices = {s: self.all_prices[s] for s in chosen if s in self.all_prices}
        volumes = {r["market"]: r["quote_volume"] for r in eligible if r["market"] in chosen}
        self.scan_report = {
            "enabled": bool(scfg.get("enabled", False)),
            "eligible_count": len(eligible),
            "pool_count": pool_count,
            "core_size": int(ucfg["size"]),
            "core_markets": core,
            "discovery_markets": discovery,
            "shadow_only": bool(scfg.get("shadow_only", True)),
            "candidates": candidates,
        }
        return chosen, prices, volumes

    def candles(self, symbol, tf_minutes, now_ts, limit=720):
        raw = self.ex.fetch_ohlcv(symbol, timeframe=TF[tf_minutes], limit=limit)
        out = [{"ts": int(r[0] // 1000), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5] or 0)} for r in raw]
        return [c for c in out if c["ts"] + tf_minutes * 60 <= now_ts]

    @staticmethod
    def is_fresh(candles, tf, now_ts):
        return bool(candles) and now_ts - (candles[-1]["ts"] + tf * 60) < 3 * tf * 60

    @staticmethod
    def new_4h_close(last_scan_ts, now_ts):
        return last_scan_ts is None or now_ts // 14400 > last_scan_ts // 14400
