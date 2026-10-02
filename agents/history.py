"""Historie-agent: haalt uurcandles van de afgelopen jaren op en bewaart ze in maandbestanden.

Kraken geeft via zijn API maar de laatste 720 candles. Voor de backtest gebruiken we daarom een beurs met een
lange openbare historie (dezelfde munten in USD of USDT). Prijsbewegingen tussen beurzen lopen vrijwel gelijk;
voor het testen van een strategie is dat ruim voldoende.

Opslag: data/history/<MUNT>/<JJJJ-MM>.json.gz. Afgesloten maanden veranderen niet meer, zodat de repo klein blijft.
"""
import gzip
import json
import time
from pathlib import Path

HOUR = 3600
SOURCES = ["binance", "kucoin", "okx", "gateio", "bitfinex", "coinbaseexchange"]
QUOTES = ["USDT", "USD", "EUR"]


class HistoryAgent:
    label = "Historie-agent"

    def __init__(self, root, log=print):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.root / "meta.json"
        self.meta = json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}
        self.log = log
        self._ex = {}

    # --- opslag ---------------------------------------------------------------------------
    def load(self, base):
        rows = []
        d = self.root / base
        if not d.exists():
            return rows
        for f in sorted(d.glob("*.json.gz")):
            with gzip.open(f, "rt") as fh:
                rows.extend(json.load(fh))
        rows.sort(key=lambda r: r[0])
        out, last = [], None
        for r in rows:
            if r[0] != last:
                out.append(r)
                last = r[0]
        return out

    def _save(self, base, rows):
        d = self.root / base
        d.mkdir(parents=True, exist_ok=True)
        months = {}
        for r in rows:
            key = time.strftime("%Y-%m", time.gmtime(r[0]))
            months.setdefault(key, []).append(r)
        for key, rs in months.items():
            path = d / f"{key}.json.gz"
            old = []
            if path.exists():
                with gzip.open(path, "rt") as fh:
                    old = json.load(fh)
            merged = {r[0]: r for r in old}
            merged.update({r[0]: r for r in rs})
            data = [merged[k] for k in sorted(merged)]
            if data != old:
                with gzip.open(path, "wt") as fh:
                    json.dump(data, fh, separators=(",", ":"))

    # --- ophalen ---------------------------------------------------------------------------
    def _exchange(self, name):
        if name not in self._ex:
            import ccxt
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            ex.load_markets()
            self._ex[name] = ex
        return self._ex[name]

    def _find_source(self, base):
        if base in self.meta:
            return self.meta[base]["exchange"], self.meta[base]["symbol"]
        for name in SOURCES:
            try:
                ex = self._exchange(name)
            except Exception as e:
                self.log(f"  {name} niet bereikbaar: {str(e)[:80]}")
                self._ex[name] = None
                continue
            if ex is None:
                continue
            for q in QUOTES:
                sym = f"{base}/{q}"
                if sym in ex.markets and ex.markets[sym].get("spot", True):
                    return name, sym
        return None, None

    def update(self, base, days):
        """Vul de historie aan tot nu. Geeft het aantal candles terug."""
        name, sym = self._find_source(base)
        if not name:
            self.log(f"{base}: geen bron met lange historie gevonden")
            return 0
        ex = self._exchange(name)
        rows = self.load(base)
        now = int(time.time())
        start = max(rows[-1][0] + HOUR, now - days * 86400) if rows else now - days * 86400
        new, since, tries = [], start * 1000, 0
        while since < (now - HOUR) * 1000 and tries < 400:
            tries += 1
            try:
                batch = ex.fetch_ohlcv(sym, "1h", since=since, limit=1000)
            except Exception as e:
                self.log(f"{base}: ophalen bij {name} mislukt ({str(e)[:80]})")
                time.sleep(2)
                if tries > 5 and not new:
                    break
                continue
            if not batch:
                since += 1000 * HOUR * 1000  # gat in de historie overslaan
                continue
            for r in batch:
                ts = int(r[0] // 1000)
                if ts + HOUR <= now and (not new or ts > new[-1][0]):
                    new.append([ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)])
            nxt = int(batch[-1][0]) + HOUR * 1000
            if nxt <= since:
                break
            since = nxt
        if new:
            self._save(base, new)
        self.meta[base] = {"exchange": name, "symbol": sym}
        self.meta_path.write_text(json.dumps(self.meta, indent=1))
        total = len(self.load(base))
        self.log(f"{base}: {len(new)} nieuwe uurcandles van {name} ({sym}), totaal {total}")
        return total
