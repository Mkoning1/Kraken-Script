"""Nieuws-agent: leest wat er in de wereld gebeurt en laat een gratis AI-model beoordelen wat het betekent.

Alles is gratis:
  - Bronnen: GDELT (wereldnieuws), RSS van cryptonieuwssites, het Kraken-blog, nieuwe noteringen op Kraken
    (zelf gedetecteerd uit de marktlijst) en de Fear & Greed-index.
  - AI: GitHub Models met de GITHUB_TOKEN van de workflow (permissie models: read). Lichte model voor de koppen,
    zware model alleen voor een koopcheck. Daglimieten blijven ruim onder de gratis grenzen.

Drie taken:
  1. Veto (direct actief): meldt het nieuws een hack, exploit, delisting, rechtszaak, depeg of insolventie van een
     munt, dan koopt de bot die munt 48 uur niet.
  2. Research: munten met een concrete nieuwe aanleiding gaan de scanner in (die volgt de scannerregel).
  3. Koopcheck: bij een koopkans geeft het zware model een tweede mening: echte aanleiding of loze pomp?
     Telt pas mee als de meting bewijst dat hij verliezers eruit haalt zonder winnaars te schrappen.

Alles wordt gemeten: de koers 24 en 72 uur na elk oordeel. Valt de AI weg (limiet, storing, geen toegang), dan werkt
een simpele woordenlijst als veto. Deze agent mag de handel nooit laten crashen: elke fout wordt opgevangen.
"""
import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (compatible; KrakenScript-NewsAgent/1.0; +https://github.com/Mkoning1/Kraken-Script)"
MODELS_URL = "https://models.github.ai/inference/chat/completions"
CATALOG_URL = "https://models.github.ai/catalog/models"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
FNG_URL = "https://api.alternative.me/fng/?limit=1"

HARD_EVENTS = {"hack", "exploit", "delisting", "rechtszaak", "depeg", "insolventie"}
EVENTS = HARD_EVENTS | {"geen", "notering", "partnerschap", "regelgeving", "upgrade", "etf", "overig"}
HARD_WORDS = ("hack", "hacked", "exploit", "exploited", "drained", "stolen", "delist", "delisted", "delisting",
              "rug pull", "rugpull", "insolvent", "insolvency", "bankrupt", "depeg", "depegged", "lawsuit",
              "sues", "charged", "halts withdrawals", "suspends withdrawals")

# Volledige namen helpen de koppen te herkennen. Onbekende munten worden op hun ticker herkend.
NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "Ripple", "ADA": "Cardano", "DOGE": "Dogecoin",
    "DOT": "Polkadot", "LINK": "Chainlink", "AVAX": "Avalanche", "LTC": "Litecoin", "BCH": "Bitcoin Cash",
    "TRX": "Tron", "ATOM": "Cosmos", "UNI": "Uniswap", "NEAR": "NEAR Protocol", "FIL": "Filecoin",
    "ICP": "Internet Computer", "APT": "Aptos", "ARB": "Arbitrum", "OP": "Optimism", "INJ": "Injective",
    "SUI": "Sui", "TIA": "Celestia", "AAVE": "Aave", "LDO": "Lido", "ETC": "Ethereum Classic", "XLM": "Stellar",
    "ALGO": "Algorand", "HBAR": "Hedera", "QNT": "Quant", "GRT": "The Graph", "SAND": "The Sandbox",
    "MANA": "Decentraland", "FET": "Fetch.ai", "RENDER": "Render", "PEPE": "Pepe", "SHIB": "Shiba Inu",
    "BONK": "Bonk", "WIF": "dogwifhat", "SEI": "Sei", "HYPE": "Hyperliquid", "ZEC": "Zcash", "XMR": "Monero",
    "TAO": "Bittensor", "ENA": "Ethena", "ONDO": "Ondo", "JUP": "Jupiter", "PUMP": "Pump.fun", "WLD": "Worldcoin",
    "KAS": "Kaspa", "PAXG": "PAX Gold", "BNB": "BNB", "POL": "Polygon", "STRK": "Starknet", "W": "Wormhole",
    "ZRO": "LayerZero", "SPX": "SPX6900", "USELESS": "Useless", "TON": "Toncoin", "TRUMP": "Official Trump",
}
# Woorden die ook als gewoon Engels voorkomen; daarop alleen als ticker met $ ervoor herkennen.
AMBIGUOUS = {"W", "S", "OP", "SUI", "SEI", "UNI", "NEAR", "POL", "QNT", "LINK", "SAND", "MANA", "PUMP", "TAO",
             "HYPE", "JUP", "ONE", "GAS", "ATOM", "DOT", "FET", "GRT", "KAS", "SPX", "TON", "TRUMP", "USELESS", "WIF",
             "BONK", "PEPE", "RENDER", "ONDO", "APT", "ENA", "TIA", "ICP", "FIL", "INJ", "ALGO", "ZRO", "STRK"}


def _hash(text):
    return hashlib.sha1(re.sub(r"\W+", " ", text.lower()).strip().encode()).hexdigest()[:16]


def _clamp(x, lo, hi, default):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default


def _json_from(text):
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("geen JSON in het antwoord")
    return json.loads(t[i:j + 1])


def mentions(title, base):
    """Noemt de kop deze munt? Volledige naam, $TICKER, of (bij duidelijke tickers) TICKER als los woord."""
    name = NAMES.get(base)
    if name and len(name) >= 4 and re.search(r"(?<![\w$])" + re.escape(name) + r"(?!\w)", title, re.I):
        return True
    if re.search(r"\$" + re.escape(base) + r"(?!\w)", title):
        return True
    return len(base) >= 3 and base not in AMBIGUOUS and re.search(r"(?<![\w$])" + re.escape(base) + r"(?!\w)", title) is not None


class Http:
    """Kleine HTTP-laag zonder extra pakketten. In tests vervangen door een nep-versie."""

    def request(self, url, data=None, headers=None, timeout=10):
        h = {"User-Agent": UA, **(headers or {})}
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read(2_000_000)
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return r.status, raw.decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            try:
                text = e.read(20_000).decode("utf-8", "replace")
            except Exception:
                text = ""
            return e.code, text, dict(e.headers or {})


class NewsAgent:
    label = "Nieuws-agent"

    def __init__(self, state, cfg, exclude_bases=(), http=None, token=None, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.http = http or Http()
        self.token = os.environ.get("GITHUB_TOKEN", "") if token is None else token
        self.clock, self.sleep = clock, sleep
        self.exclude = set(exclude_bases)
        self.s = state.setdefault("news", {})
        for key, default in (("seen", []), ("headlines", []), ("coins", {}), ("vetoes", {}), ("picks", {}),
                             ("trackers", []), ("usage", {}), ("models", {}), ("alerts", {})):
            self.s.setdefault(key, default)
        self.started = clock()
        self.checks_this_run = 0
        self.notes = []
        self._last_gdelt = 0.0

    # ------------------------------------------------------------------ hulpjes
    def _left(self):
        return self.cfg.get("time_budget_sec", 75) - (self.clock() - self.started)

    def _get(self, url, timeout=8):
        status, text, _ = self.http.request(url, timeout=min(timeout, max(2, self._left())))
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return text

    def _error(self, where, e):
        self.s["last_error"] = f"{where}: {str(e)[:160]}"
        self.s["last_error_ts"] = int(self.clock())

    def _usage(self, now_ts):
        day = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d")
        if self.s["usage"].get("day") != day:
            self.s["usage"] = {"day": day, "light": 0, "heavy": 0, "failed": 0}
        return self.s["usage"]

    def ai_ready(self, now_ts, kind):
        if not self.token or not self.cfg.get("ai", True):
            return False
        if (self.s.get("pause_until") or 0) > now_ts:
            return False
        cap = self.cfg.get(f"daily_cap_{kind}", 100)
        return self._usage(now_ts)[kind] < cap and self._left() > 15

    # ------------------------------------------------------------------ bronnen
    def _rss(self, url, source):
        root = ET.fromstring(self._get(url).encode("utf-8", "replace"))
        out = []
        for it in root.iter():
            tag = it.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            title = link = when = None
            for ch in it:
                t = ch.tag.split("}")[-1]
                if t == "title":
                    title = (ch.text or "").strip()
                elif t == "link":
                    link = (ch.text or "").strip() or ch.attrib.get("href")
                elif t in ("pubDate", "published", "updated") and not when:
                    when = (ch.text or "").strip()
            if not title:
                continue
            ts = None
            if when:
                try:
                    ts = int(parsedate_to_datetime(when).timestamp())
                except Exception:
                    try:
                        ts = int(datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts = None
            out.append({"title": title, "url": link, "src": source, "ts": ts})
        return out[:30]

    def _gdelt(self, query, timespan="1h", maxrecords=15):
        # GDELT knijpt gedeelde cloud-adressen (zoals GitHub) af: na een 429 een half uur met rust laten.
        if (self.s.get("gdelt_pause_until") or 0) > self.clock():
            raise RuntimeError("pauze na limiet")
        wait = 5.5 - (self.clock() - self._last_gdelt)
        if wait > 0:
            if wait > self._left() - 10:
                raise RuntimeError("geen tijd meer voor GDELT")
            self.sleep(wait)
        self._last_gdelt = self.clock()
        url = GDELT_URL + "?" + urllib.parse.urlencode({
            "query": query, "mode": "artlist", "format": "json", "maxrecords": maxrecords,
            "timespan": timespan, "sort": "datedesc"})
        status, text, _ = self.http.request(url, timeout=min(12, max(2, self._left())))
        if status == 429:
            self.s["gdelt_pause_until"] = int(self.clock()) + 1800
            raise RuntimeError("HTTP 429, pauze 30 min")
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        if not text.strip().startswith("{"):
            raise RuntimeError(text.strip()[:80] or "leeg antwoord")
        out = []
        for a in (json.loads(text).get("articles") or []):
            ts = None
            try:
                ts = int(datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                pass
            if a.get("title"):
                out.append({"title": a["title"].strip(), "url": a.get("url"), "src": a.get("domain") or "GDELT", "ts": ts,
                            "gdelt": True})
        return out

    def _listings(self, all_prices, now_ts):
        current = sorted(m for m in all_prices if m.endswith("/EUR"))
        known = set(self.s.get("known_markets") or [])
        self.s["known_markets"] = sorted(known | set(current))  # nooit vergeten: geen valse 'nieuwe' noteringen
        if not known:
            return []
        return [{"title": f"Nieuwe notering op Kraken: {m}", "url": "https://pro.kraken.com", "src": "Kraken (marktlijst)",
                 "ts": now_ts, "listing": m} for m in current if m not in known]

    def _fng(self):
        d = (json.loads(self._get(FNG_URL)).get("data") or [{}])[0]
        if d.get("value") is not None:
            self.s["fng"] = {"value": int(d["value"]), "label": d.get("value_classification"), "ts": int(self.clock())}

    # ------------------------------------------------------------------ AI (GitHub Models)
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _models(self, now_ts):
        """Kiest eens per dag de modellen uit de catalogus; valt terug op de lijst in config.json."""
        m = self.s["models"]
        day = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d")
        if m.get("day") != day:
            m.update(day=day, light=None, heavy=None, catalog=False)
            try:
                status, text, _ = self.http.request(CATALOG_URL, headers=self._headers(), timeout=8)
                m["catalog_status"] = status
                if status == 200:
                    tiers = {x.get("id"): (x.get("rate_limit_tier") or "") for x in json.loads(text) if x.get("id")}
                    for kind, tier in (("light", "low"), ("heavy", "high")):
                        pref = [x for x in self.cfg.get(f"{kind}_models", []) if x in tiers]
                        alt = [i for i, t in tiers.items() if t == tier and i.startswith("openai/")]
                        m[kind] = (pref or alt or [None])[0]
                    m["catalog"] = True
            except Exception as e:
                self._error("modelcatalogus", e)
        return m

    def _ai(self, kind, system, user, now_ts, max_tokens=1200):
        """Eén AI-aanroep. Geeft een dict of None; zet bij limieten/storingen een pauze."""
        if not self.ai_ready(now_ts, kind):
            return None
        chosen = self._models(now_ts).get(kind)
        options = [chosen] if chosen else []
        options += [x for x in self.cfg.get(f"{kind}_models", []) if x not in options]
        usage = self._usage(now_ts)
        for model in options:
            if self._left() < 12:
                return None
            payload = {"model": model, "temperature": 0, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            try:
                status, text, headers = self.http.request(MODELS_URL, data=payload, headers=self._headers(),
                                                          timeout=min(45, max(5, self._left() - 5)))
            except Exception as e:  # time-out of netwerkfout: deze ronde zonder AI
                self._error(f"GitHub Models ({model})", e)
                break
            if status == 200:
                usage[kind] += 1
                self.s["models"][kind] = model
                try:
                    content = json.loads(text)["choices"][0]["message"]["content"]
                    out = _json_from(content)
                    self.s["last_ai_ok"] = now_ts
                    return out
                except Exception as e:
                    usage["failed"] = usage.get("failed", 0) + 1
                    self._error(f"AI-antwoord ({model})", e)
                    h = {k.lower(): v for k, v in (headers or {}).items()}
                    self.s["debug_ai"] = {"ts": now_ts, "model": model, "len": len(text or ""), "begin": (text or "")[:400],
                                          "type": h.get("content-type"), "encoding": h.get("content-encoding")}
                    return None
            if status == 429:
                retry = _clamp((headers or {}).get("Retry-After") or (headers or {}).get("retry-after"), 60, 86400, 3600)
                self.s["pause_until"] = now_ts + int(retry)
                self._error("GitHub Models", f"limiet bereikt, pauze {int(retry // 60)} min")
                return None
            if status in (401, 403):
                self.s["pause_until"] = now_ts + 6 * 3600
                self._error("GitHub Models", f"geen toegang (HTTP {status}); staat 'models: read' in de workflow?")
                return None
            self._error(f"GitHub Models ({model})", f"HTTP {status} {text[:100]}")
            if status not in (400, 404, 422):
                break
        usage["failed"] = usage.get("failed", 0) + 1
        self.s["pause_until"] = now_ts + 15 * 60
        return None

    # ------------------------------------------------------------------ verversen (elke kwartier)
    def due(self, now_ts):
        every = int(self.cfg.get("every_minutes", 15)) * 60
        last = self.s.get("last_refresh")
        return last is None or now_ts // every > int(last) // every

    def refresh(self, now_ts, all_prices, followed):
        """Haalt nieuws op en laat het beoordelen. Geeft nooit een fout door."""
        if not self.due(now_ts):
            return
        self.s["last_refresh"] = now_ts
        items, counts = [], {}
        try:
            items += self._listings(all_prices, now_ts)
        except Exception as e:
            self._error("noteringen", e)
        for feed in self.cfg.get("rss", []):
            if self._left() < 30:
                break
            try:
                got = self._rss(feed["url"], feed["name"])
                for it in got:
                    it["world"] = bool(feed.get("world"))
                counts[feed["name"]] = len(got)
                items += got
            except Exception as e:
                counts[feed["name"]] = 0
                self._error(feed["name"], e)
        queries = self.cfg.get("gdelt_queries", [])
        # Eén GDELT-vraag per kwartier, om de beurt (crypto, economie, geopolitiek): blijft onder hun limiet.
        if queries:
            i = int(self.s.get("gdelt_i", 0))
            self.s["gdelt_i"] = i + 1
            queries = [queries[i % len(queries)]]
        for q in queries:
            if self._left() < 30:
                break
            try:
                got = self._gdelt(q["query"])
                counts[f"GDELT {q['name']}"] = len(got)
                items += got
            except Exception as e:
                counts[f"GDELT {q['name']}"] = 0
                self._error(f"GDELT {q['name']}", e)
        try:
            self._fng()
        except Exception as e:
            self._error("Fear & Greed", e)
        self.s["sources"] = counts

        seen = set(self.s["seen"])
        fresh = []
        for it in items:
            h = _hash(it["title"])
            if h in seen:
                continue
            seen.add(h)
            it["id"] = h
            it["ts"] = it.get("ts") or now_ts
            if now_ts - it["ts"] > 2 * 86400:  # oud nieuws dat een feed opnieuw toont
                continue
            it["title"] = it["title"][:180]
            fresh.append(it)
        self.s["seen"] = (self.s["seen"] + [it["id"] for it in fresh])[-1200:]
        self.s["headlines"] = sorted(self.s["headlines"] + fresh, key=lambda x: x["ts"])[-250:]
        self.s["last_new"] = len(fresh)
        if not fresh:
            self.s["last_ok"] = now_ts
            return

        followed = [b for b in followed if b not in self.exclude]
        for it in fresh:  # nieuwe noteringen zijn zelf een aanleiding
            if it.get("listing"):
                self._pick(it["listing"], "Nieuw op Kraken", now_ts, it)
        if self.ai_ready(now_ts, "light"):
            self._analyse(fresh, followed, all_prices, now_ts)
        else:
            self._keyword_veto(fresh, followed, all_prices, now_ts, why="AI niet beschikbaar")
        self.s["last_ok"] = now_ts

    def _analyse(self, fresh, followed, all_prices, now_ts):
        def prio(it):
            hit = any(mentions(it["title"], b) for b in followed)
            return (not hit, bool(it.get("gdelt") or it.get("world")), -it["ts"])
        batch = sorted(fresh, key=prio)[:int(self.cfg.get("max_headlines_per_call", 50))]
        ids = {str(i + 1): it for i, it in enumerate(batch)}
        lines = "\n".join(f"{i} | {datetime.fromtimestamp(it['ts'], timezone.utc):%d-%m %H:%M} | {it['src']} | {it['title']}"
                          for i, it in ids.items())
        fng = self.s.get("fng") or {}
        system = ("Je bent de nieuws-agent van een cryptotradingbot op Kraken (spot, in euro's). Opdracht van de eigenaar: "
                  "lees alles wat er in de wereld gebeurt en bepaal zelf wat belangrijk is voor de munten die we volgen en "
                  "voor de cryptomarkt als geheel. Wees nuchter: de meeste koppen zijn ruis. Verzin niets en baseer elk "
                  "oordeel alleen op de gegeven koppen. Antwoord uitsluitend met één JSON-object.")
        user = (f"Munten die we volgen: {', '.join(followed) or 'geen'}.\n"
                f"Marktstemming (Fear & Greed): {fng.get('value', 'onbekend')} {fng.get('label') or ''}.\n"
                f"Nieuwe koppen (id | tijd UTC | bron | kop):\n{lines}\n\n"
                "Geef precies dit JSON-formaat:\n"
                '{"markt": {"richting": -2..2, "reden": "max 20 woorden"},\n'
                ' "munten": [{"munt": "TICKER", "richting": -2..2, "impact": 1..3, "termijn": "uren|dagen|weken",'
                ' "zekerheid": 0..1, "gebeurtenis": "geen|hack|exploit|delisting|rechtszaak|depeg|insolventie|notering|'
                'partnerschap|regelgeving|upgrade|etf|overig", "reden": "max 25 woorden", "bronnen": [id]}],\n'
                ' "kansen": [{"munt": "TICKER", "aanleiding": "max 20 woorden", "bronnen": [id]}]}\n'
                "Regels:\n"
                "- richting: verwachte invloed op de koers van die munt in de komende dagen (-2 sterk negatief, 2 sterk positief).\n"
                "- Noem alleen munten die in de koppen staan of er direct door geraakt worden. Wat vooral de hele markt raakt "
                "(rente, oorlog, handelsconflict, ETF-stromen voor bitcoin) hoort bij 'markt'.\n"
                "- gebeurtenis hack, exploit, delisting, rechtszaak, depeg of insolventie alleen als een kop dat letterlijk "
                "meldt over die munt of het project erachter.\n"
                "- kansen: alleen munten met een concrete nieuwe aanleiding (notering op een grote beurs, groot partnerschap, "
                "ETF, belangrijke upgrade) die nog niet ingeprijsd lijkt. Ook munten die we nog niet volgen. Mag leeg.\n"
                "- Gebruik tickers zoals op beurzen (BTC, ETH, SOL). Geen relevante koppen? Geef lege lijsten.")
        out = self._ai("light", system, user, now_ts, max_tokens=1500)
        if out is None:
            self._keyword_veto(fresh, followed, all_prices, now_ts, why="AI gaf geen bruikbaar antwoord")
            return
        mk = out.get("markt") or {}
        self.s["market"] = {"richting": int(_clamp(mk.get("richting"), -2, 2, 0)),
                            "reden": str(mk.get("reden") or "")[:200], "ts": now_ts}
        bases = {m.split("/")[0] for m in all_prices}
        for c in (out.get("munten") or [])[:25]:
            base = str(c.get("munt") or "").upper().strip().lstrip("$")
            if not base or base not in bases or base in self.exclude:
                continue
            src = next((ids[str(i)] for i in (c.get("bronnen") or []) if str(i) in ids), None)
            row = {"richting": int(_clamp(c.get("richting"), -2, 2, 0)), "impact": int(_clamp(c.get("impact"), 1, 3, 1)),
                   "termijn": str(c.get("termijn") or "dagen")[:10], "zekerheid": round(_clamp(c.get("zekerheid"), 0, 1, 0.5), 2),
                   "gebeurtenis": c.get("gebeurtenis") if c.get("gebeurtenis") in EVENTS else "overig",
                   "reden": str(c.get("reden") or "")[:220], "ts": now_ts,
                   "title": src and src["title"], "url": src and src.get("url"), "src": src and src["src"]}
            self.s["coins"][base] = row
            market = f"{base}/EUR"
            if row["richting"] != 0 and market in all_prices:
                self.track("score", market, all_prices[market], now_ts, row["richting"])
            if (row["gebeurtenis"] in HARD_EVENTS and row["richting"] <= -1
                    and row["zekerheid"] >= self.cfg.get("veto_min_confidence", 0.6)):
                self._veto(market, row["reden"] or row["gebeurtenis"], now_ts, all_prices, src, "AI",
                           hours=self.cfg.get("veto_hours", 48))
        for k in (out.get("kansen") or [])[:5]:
            base = str(k.get("munt") or "").upper().strip().lstrip("$")
            src = next((ids[str(i)] for i in (k.get("bronnen") or []) if str(i) in ids), None)
            if base and base not in self.exclude:
                self._pick(f"{base}/EUR", str(k.get("aanleiding") or "")[:160], now_ts, src, all_prices)

    # Ketens worden in hack-koppen vaak alleen als plaats genoemd ("hack op een Solana-project"); de woordenlijst
    # is daar te grof voor. Die laat de AI beoordelen.
    CHAINS = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "TRX", "AVAX", "DOT", "ARB", "OP", "POL", "SUI", "TON", "NEAR",
              "APT", "SEI", "ATOM", "HBAR", "ALGO", "XLM"}

    def _keyword_veto(self, fresh, followed, all_prices, now_ts, why):
        for it in fresh:
            low = it["title"].lower()
            word = next((w for w in HARD_WORDS if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", low)), None)
            if not word:
                continue
            for base in followed:
                if base not in self.CHAINS and mentions(it["title"], base):
                    self._veto(f"{base}/EUR", f"Woordenlijst ({why}): '{word}' in een kop over {base}", now_ts,
                               all_prices, it, "woordenlijst", hours=24)

    def _veto(self, market, reason, now_ts, all_prices, src, by, hours):
        old = self.s["vetoes"].get(market)
        self.s["vetoes"][market] = {"until": now_ts + int(hours * 3600), "reason": reason[:220], "by": by, "ts": now_ts,
                                    "title": src and src.get("title"), "url": src and src.get("url")}
        if not old or old["until"] < now_ts:
            self.notes.append(f"Nieuws-agent: {market} {hours} uur niet kopen. {reason}")
            if market in all_prices:
                self.track("veto", market, all_prices[market], now_ts, by)

    def _pick(self, market, reason, now_ts, src, all_prices=None):
        dcfg = self.cfg.get("discovery", {})
        if not dcfg.get("enabled", True):
            return
        if all_prices is not None and market not in all_prices:
            return
        picks = self.s["picks"]
        new = market not in picks or picks[market]["until"] < now_ts
        picks[market] = {"until": now_ts + int(dcfg.get("days", 3) * 86400), "reason": reason, "ts": now_ts,
                         "title": src and src.get("title"), "url": src and src.get("url")}
        live = sorted((m for m in picks if picks[m]["until"] >= now_ts), key=lambda m: -picks[m]["ts"])
        for m in live[int(dcfg.get("max", 3)):]:
            picks.pop(m, None)
        if new and market in picks:
            self.notes.append(f"Nieuws-agent: {market} gaat de scanner in. {reason}")

    # ------------------------------------------------------------------ gebruik door de handelsronde
    def veto_for(self, market, now_ts):
        v = self.s["vetoes"].get(market)
        return v if v and v["until"] > now_ts else None

    def picks(self, now_ts):
        return [m for m, p in self.s["picks"].items() if p["until"] > now_ts]

    def coin_headlines(self, base, now_ts, hours=72):
        return [h for h in self.s["headlines"] if now_ts - h["ts"] <= hours * 3600 and mentions(h["title"], base)][-12:]

    def research_check(self, market, decision, ctx, price, now_ts):
        """Tweede mening van het zware model bij een koopkans. None als er geen AI beschikbaar is."""
        per_run = int(self.cfg.get("checks_per_run_live" if self.check_is_live() else "checks_per_run", 2))
        if self.checks_this_run >= per_run or not self.ai_ready(now_ts, "heavy"):
            return None
        self.checks_this_run += 1
        base = market.split("/")[0]
        found = self.coin_headlines(base, now_ts)
        name = NAMES.get(base)
        if name and " " in name:
            query = f'"{name}"'
        elif name and base not in AMBIGUOUS and len(name) >= 5:
            query = name
        elif len(name or base) >= 3:
            query = f"{name or base} crypto"
        else:
            query = None
        if query and self._left() > 35:
            try:
                extra = self._gdelt(f"{query} sourcelang:english", timespan="72h", maxrecords=10)
                have = {_hash(h["title"]) for h in found}
                found += [x for x in extra if _hash(x["title"]) not in have][:8]
            except Exception as e:
                self._error("GDELT (koopcheck)", e)
        sig = (ctx or {}).get("sig") or {}
        nums = {k: round(float(v), 6) for k, v in sig.items()
                if isinstance(v, (int, float)) and k in ("close", "rsi", "atr", "ema20", "ema50", "ema200", "prev_high")}
        lines = "\n".join(f"- {datetime.fromtimestamp(h['ts'], timezone.utc):%d-%m %H:%M} | {h['src']} | {h['title'][:160]}"
                          for h in found[-12:]) or "Geen koppen gevonden."
        fng = self.s.get("fng") or {}
        mkt = self.s.get("market") or {}
        system = ("Je bent de research-agent van een cryptotradingbot. Je beoordeelt één koopkans: zit er een echte "
                  "aanleiding achter de koersbeweging, of lijkt het een loze pomp die snel terugvalt? Wees kritisch: valse "
                  "uitbraken komen vaker voor dan echte. Verzin geen nieuws. Antwoord uitsluitend met één JSON-object.")
        user = (f"Munt: {base} ({market}). Koers nu: {price}.\n"
                f"Koopreden van de strategie: {decision.get('reason')}\n"
                f"Desk: {decision.get('horizon')}, score {round(float(decision.get('score') or 0), 2)}.\n"
                f"Indicatoren: {json.dumps(nums)}\n"
                f"Marktstemming (Fear & Greed): {fng.get('value', 'onbekend')} {fng.get('label') or ''}. "
                f"Markt volgens het nieuws: richting {mkt.get('richting', 0)}, {mkt.get('reden', 'geen beeld')}.\n"
                f"Koppen over deze munt (laatste 72 uur):\n{lines}\n\n"
                'Geef: {"oordeel": "doorlaten|tegenhouden", "zekerheid": 0..1, '
                '"aanleiding": "max 15 woorden, of geen", "reden": "max 30 woorden"}')
        out = self._ai("heavy", system, user, now_ts, max_tokens=300)
        if out is None:
            return None
        verdict = {"oordeel": "tegenhouden" if str(out.get("oordeel", "")).lower().startswith("tegen") else "doorlaten",
                   "zekerheid": round(_clamp(out.get("zekerheid"), 0, 1, 0.5), 2),
                   "aanleiding": str(out.get("aanleiding") or "geen")[:120], "reden": str(out.get("reden") or "")[:240],
                   "nieuws": len(found), "model": self.s["models"].get("heavy"), "ts": now_ts}
        self.track("check", market, price, now_ts, verdict["oordeel"])
        return verdict

    def check_is_live(self):
        mode = self.cfg.get("research_check_live", "auto")
        if mode in (True, False):
            return mode
        return bool(self.s.get("scorecard", {}).get("trades", {}).get("goal_met"))

    def held_alerts(self, held, now_ts):
        """Hard nieuws over een munt die we hebben: één melding per veto. Verkopen op nieuws wordt eerst gemeten."""
        out = []
        for m in held:
            v = self.veto_for(m, now_ts)
            if v and self.s["alerts"].get(m) != v["ts"]:
                self.s["alerts"][m] = v["ts"]
                out.append((m, v))
        return out

    # ------------------------------------------------------------------ meten
    def track(self, kind, market, price, ts, value):
        if price:
            self.s["trackers"].append({"k": kind, "m": market, "ts": int(ts), "p": float(price), "v": value,
                                       "r24": None, "r72": None})
            self.s["trackers"] = self.s["trackers"][-600:]

    def evaluate(self, now_ts, all_prices, trades):
        for t in self.s["trackers"]:
            p = all_prices.get(t["m"])
            if not p:
                continue
            if t["r24"] is None and now_ts >= t["ts"] + 86400:
                t["r24"] = round((p / t["p"] - 1) * 100, 2)
            if t["r72"] is None and now_ts >= t["ts"] + 3 * 86400:
                t["r72"] = round((p / t["p"] - 1) * 100, 2)
        for coll in ("vetoes", "picks"):
            self.s[coll] = {m: v for m, v in self.s[coll].items() if v["until"] > now_ts - 7 * 86400}
        if len(self.s["coins"]) > 60:
            keep = sorted(self.s["coins"], key=lambda b: -self.s["coins"][b]["ts"])[:60]
            self.s["coins"] = {b: self.s["coins"][b] for b in keep}
        self.s["scorecard"] = self.scorecard(trades)

    def scorecard(self, trades):
        def grp(kind, test):
            rows = [t for t in self.s["trackers"] if t["k"] == kind and test(t["v"])]
            r24 = [t["r24"] for t in rows if t["r24"] is not None]
            r72 = [t["r72"] for t in rows if t["r72"] is not None]
            return {"n": len(rows), "gemeten": len(r24),
                    "r24": round(sum(r24) / len(r24), 2) if r24 else None,
                    "r72": round(sum(r72) / len(r72), 2) if r72 else None}
        checked = [t for t in trades if t.get("news_check")][-50:]
        losers = [t for t in checked if t["pnl"] <= 0]
        winners = [t for t in checked if t["pnl"] > 0]
        lf = sum(1 for t in losers if t["news_check"]["oordeel"] == "tegenhouden")
        wf = sum(1 for t in winners if t["news_check"]["oordeel"] == "tegenhouden")
        need = int(self.cfg.get("check_goal_trades", 50))
        goal = (len(checked) >= need and losers and winners
                and lf / len(losers) >= self.cfg.get("check_goal_losers", 0.2)
                and wf / len(winners) <= self.cfg.get("check_goal_winners", 0.1))
        return {
            "koopcheck_doorlaten": grp("check", lambda v: v == "doorlaten"),
            "koopcheck_tegenhouden": grp("check", lambda v: v == "tegenhouden"),
            "nieuws_positief": grp("score", lambda v: isinstance(v, int) and v > 0),
            "nieuws_negatief": grp("score", lambda v: isinstance(v, int) and v < 0),
            "veto": grp("veto", lambda v: True),
            "trades": {"gecheckt": len(checked), "nodig": need, "verliezers": len(losers), "verliezers_tegengehouden": lf,
                       "winnaars": len(winners), "winnaars_tegengehouden": wf, "goal_met": bool(goal)},
        }

    # ------------------------------------------------------------------ verslag en dashboard
    def narrative(self, now_ts):
        u = self._usage(now_ts)
        ai = ("AI actief" if self.ai_ready(now_ts, "light") or (self.s.get("last_ai_ok") or 0) > now_ts - 3600
              else "AI niet beschikbaar, woordenlijst actief")
        vetoes = [m for m in self.s["vetoes"] if self.veto_for(m, now_ts)]
        line = (f"Nieuws-agent: {ai} ({u.get('light', 0)} lichte en {u.get('heavy', 0)} zware beoordelingen vandaag). "
                f"{len(vetoes)} veto('s) actief" + (f": {', '.join(vetoes)}" if vetoes else "") + ".")
        return [line] + self.notes

    def dashboard(self, now_ts):
        u = self._usage(now_ts)
        coins = sorted(({"base": b, **v} for b, v in self.s["coins"].items()), key=lambda x: -x["ts"])[:15]
        return {
            "enabled": True,
            "ai": {"token": bool(self.token), "light_model": self.s["models"].get("light"),
                   "heavy_model": self.s["models"].get("heavy"), "used_today": u,
                   "caps": {"light": self.cfg.get("daily_cap_light"), "heavy": self.cfg.get("daily_cap_heavy")},
                   "paused_until": self.s.get("pause_until") if (self.s.get("pause_until") or 0) > now_ts else None,
                   "last_ok": self.s.get("last_ai_ok")},
            "last_refresh": self.s.get("last_refresh"), "last_ok": self.s.get("last_ok"),
            "last_error": self.s.get("last_error"), "last_error_ts": self.s.get("last_error_ts"),
            "sources": self.s.get("sources", {}), "fng": self.s.get("fng"), "market": self.s.get("market"),
            "vetoes": [{"market": m, **v} for m, v in self.s["vetoes"].items() if v["until"] > now_ts],
            "picks": [{"market": m, **v} for m, v in self.s["picks"].items() if v["until"] > now_ts],
            "coins": coins,
            "headlines": [{k: h.get(k) for k in ("title", "src", "ts", "url")} for h in self.s["headlines"][-20:][::-1]],
            "scorecard": self.s.get("scorecard"),
            "koopcheck_live": self.check_is_live(),
        }
