#!/usr/bin/env python3
"""
Kraken Multi-Coin Momentum-Breakout Bot (CCXT)
================================================
Strategie: Donchian-breakout entry + TWEETRAPS ATR-trailing-stop exit.
Scant een watchlist van meerdere coins tegelijk en stapt in op de sterkste
breakout die zich aandient -- in plaats van te wachten op een enkel vast
paar. Doel: meer kansen om een echte trend te pakken, zonder dat er meer
risico per trade bijkomt (nog steeds maar 1 positie tegelijk, hele saldo
erin -- bij €10 kapitaal heeft spreiden over meerdere posities toch geen zin).

Hoe de tweetraps-stop werkt:
  - Zolang een positie nog niet bewezen heeft dat het een echte trend is
    (winst < RUNNER_THRESHOLD_ATR keer de ATR), staat de trailing stop KRAP
    (ATR_TRAIL_MULT_INITIAL) -- twijfelachtige trades worden snel en
    goedkoop afgekapt.
  - Zodra de winst die drempel passeert, wordt de trade een "runner" en
    springt de stop naar BREED (ATR_TRAIL_MULT_RUNNER) voor de rest van de
    trade -- dan krijgt-ie veel meer ruimte om door te lopen.

Hoe het scannen werkt:
  - Vóór elke scan checkt de bot eerst of er al een saldo van een watchlist-
    coin op de rekening staat dat hij nog niet als eigen positie kent (bv.
    handmatig gekocht). Zo ja: adopteert hij dat als positie, met een
    instapprijs herleid uit de echte trade-historie op Kraken, en gaat 'm
    vanaf dan gewoon beheren (incl. verkopen via de trailing stop). Dit is
    een read-only herkenningsstap -- er wordt geen order voor geplaatst, dus
    dit gebeurt ook gewoon in DRY_RUN.
  - Is er niks te adopteren, dan vraagt de bot Kraken zelf welke EUR-paren
    er zijn en pakt de top WATCHLIST_SIZE op 24u-volume (stablecoins
    uitgesloten) -- geen vaste lijst meer die met de hand moet worden
    bijgehouden.
  - Zolang er geen positie open is, checkt de bot elke cyclus ALLE paren in
    die watchlist op een Donchian-breakout.
  - Breken er meerdere tegelijk uit, dan kiest de bot de sterkste (grootste
    uitbraak, gemeten in ATR's boven het kanaal) -- niet zomaar de eerste
    in de lijst.
  - Zodra er 1 positie open is, wordt alleen dat ene paar nog gevolgd totdat
    de trailing stop raakt. Pas daarna wordt er weer verder gescand.

Timeframe : 4h (beslissingen op de laatst GESLOTEN candle)

DEPLOYMENT: gebouwd om via GitHub Actions te draaien. Het script doet 1
cyclus en stopt -- de cron-schedule in de workflow (elke 15 min) regelt
het herhalen, niet een while-loop hier. Status (bot_state.json + STATUS.md)
wordt aan het eind teruggeschreven naar de repo zodat de volgende run weet
waar hij gebleven was, en jij overal kunt zien wat de bot aan het doen is.

API-keys komen uit de omgevingsvariabelen KRAKEN_API_KEY / KRAKEN_API_SECRET
(GitHub Secrets in het workflow-bestand) -- NOOIT hardcoden in dit bestand,
ook niet in een private repo. Zie de bijgeleverde workflow + instructies.

LET OP -- HOOG RISICO CONFIGURATIE:
Geen trendfilter, volledige positiesizing, brede stop. Dit kan je volledige
inzet verliezen. DRY_RUN staat standaard op True: de bot logt dan alleen wat
hij zou doen, zonder echte orders te plaatsen. Laat 'm zo een paar dagen
meelezen met de markt voordat je 'm op live zet.
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

import ccxt
import pandas as pd

# ============================================================================
#  VEILIGHEID -- eerst hier checken voordat je iets anders aanraakt
# ============================================================================
DRY_RUN = False  # LIVE: de bot plaatst nu echte orders met echt geld.

# ============================================================================
#  API-KEYS -- komen uit GitHub Secrets, staan NERGENS in dit bestand
# ============================================================================
API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")

# Watchlist wordt NIET meer hardgecodeerd -- elke run vraagt de bot Kraken zelf
# welke EUR-paren er zijn en pakt de top WATCHLIST_SIZE op 24u-volume. Groeit
# en beweegt dus vanzelf mee met de markt, in plaats van een vaste lijst die
# ik ooit met de hand samenstelde. Zie discover_watchlist() verderop.
WATCHLIST_SIZE   = 25
STABLECOIN_BASES = {"USDT", "USDC", "DAI", "USD", "GBP", "PYUSD", "TUSD", "USDG", "EURT", "EURR"}
FALLBACK_WATCHLIST = ["ADA/EUR", "SOL/EUR", "DOT/EUR", "LINK/EUR", "AVAX/EUR"]  # als volume-ophalen faalt

WATCHLIST  = list(FALLBACK_WATCHLIST)  # wordt bij elke run overschreven door discover_watchlist()
TIMEFRAME  = "4h"

# --- Strategie-parameters --------------------------------------------------
BREAKOUT_LOOKBACK  = 55     # candles (~9 dagen op 4h) -- bewust lang: minder signalen,
                             # maar elk signaal betekent ook echt iets.
ATR_PERIOD         = 14

ATR_TRAIL_MULT_INITIAL = 2.0   # smalle stop zolang de trade zich niet bewezen heeft
ATR_TRAIL_MULT_RUNNER  = 6.0   # brede stop zodra de trade een "runner" is geworden
RUNNER_THRESHOLD_ATR   = 3.0   # winst (in ATR's) vanaf entry waarna een trade een runner wordt

RSI_PERIOD         = 14
RSI_MIN_FOR_ENTRY  = 50     # lichte momentum-confirmatie. Zet op 0 om uit te schakelen.
MIN_ORDER_EUR      = 5.0    # Kraken cost-minimum voor EUR-paren

STATE_FILE    = Path(__file__).with_name("bot_state.json")
STATUS_FILE   = Path(__file__).with_name("README.md")   # README rendert automatisch
                                                          # op de repo-hoofdpagina, geen
                                                          # extra klik nodig om 'm te zien

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("multi-momentum-bot")


# ============================================================================
#  STATE  (overleeft elke run -- elke GitHub Actions-run start op een schone
#  VM, dus zonder dit terug te schrijven naar de repo zou de bot bij elke
#  cyclus zijn geheugen kwijtraken)
# ============================================================================
def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        state.setdefault("current_price", None)
        state.setdefault("current_atr", None)
        state.setdefault("watchlist_snapshot", {})
        state.setdefault("trade_history", [])
        return state
    return {
        "in_position": False,
        "symbol": None,
        "entry_price": None,
        "peak_price": None,
        "current_price": None,
        "current_atr": None,
        "position_size": None,
        "is_runner": False,
        "last_candle_ts": {},   # {symbol: ts van laatst-verwerkte candle}
        "watchlist_snapshot": {},  # {symbol: {price, donchian_high, checked_at}} -- voor het dashboard
        "trade_history": [],       # afgeronde trades, nieuwste laatst, voor het dashboard
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ============================================================================
#  EXCHANGE
# ============================================================================
def build_exchange():
    if not API_KEY or not API_SECRET:
        log.error(
            "KRAKEN_API_KEY / KRAKEN_API_SECRET niet gevonden in de omgeving. "
            "Check de GitHub Secrets en de 'env:' sectie in de workflow."
        )
        sys.exit(1)
    return ccxt.kraken({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })


# ============================================================================
#  INDICATOREN
# ============================================================================
def fetch_data(exchange, symbol):
    limit = max(BREAKOUT_LOOKBACK, ATR_PERIOD, RSI_PERIOD) + 10
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
    return pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])


def add_indicators(df):
    df = df.copy()

    # Donchian-kanaal: hoogste high van de N candles VOOR de huidige (shift(1)
    # voorkomt dat de candle zichzelf meetelt -> geen lookahead bias)
    df["donchian_high"] = df["high"].shift(1).rolling(BREAKOUT_LOOKBACK).max()

    # RSI (simpele SMA-variant, geen Wilder-smoothing -- consistent en licht)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(ATR_PERIOD).mean()

    return df


def latest_closed_candle(exchange, symbol):
    df = add_indicators(fetch_data(exchange, symbol))
    return df.iloc[-2]  # laatste VOLLEDIG gesloten candle


# ============================================================================
#  TRADING
# ============================================================================
def base_currency(symbol):
    return symbol.split("/")[0]


def fmt_price(x):
    """Leesbaar loggen ongeacht schaal: 4 decimalen voor SOL/DOT (>1), meer voor
    ADA-achtige centen, nog meer voor memecoins zoals PEPE/SHIB (~0.00001)."""
    ax = abs(x)
    if ax == 0:
        return "0"
    if ax >= 1:
        return f"{x:.4f}"
    if ax >= 0.0001:
        return f"{x:.6f}"
    return f"{x:.10f}"


def get_eur_balance(exchange):
    return exchange.fetch_balance()["free"].get("EUR", 0.0)


def get_base_balance(exchange, symbol):
    return exchange.fetch_balance()["free"].get(base_currency(symbol), 0.0)


def place_buy(exchange, symbol, eur_amount, price):
    """Retourneert de gekochte hoeveelheid, of None als de trade werd overgeslagen."""
    if eur_amount < MIN_ORDER_EUR:
        log.warning(f"{symbol}: saldo €{eur_amount:.2f} onder orderminimum €{MIN_ORDER_EUR}. Sla trade over.")
        return None

    market = exchange.market(symbol)
    min_amount = market["limits"]["amount"]["min"] or 0
    raw_amount = eur_amount / price
    amount = float(exchange.amount_to_precision(symbol, raw_amount))

    if amount < min_amount:
        log.warning(f"{symbol}: hoeveelheid {amount} onder Kraken-minimum {min_amount}. Sla trade over.")
        return None

    if DRY_RUN:
        log.info(f"[DRY RUN] Zou kopen: {amount} {base_currency(symbol)} (~€{eur_amount:.2f}) @ ~€{fmt_price(price)}")
    else:
        exchange.create_market_buy_order(symbol, amount)
        log.info(f"BUY  {amount} {base_currency(symbol)} (~€{eur_amount:.2f}) @ ~€{fmt_price(price)}")

    return amount


def place_sell(exchange, symbol, amount):
    """Retourneert de verkochte hoeveelheid, of None als er niets te verkopen viel."""
    amount = float(exchange.amount_to_precision(symbol, amount))
    if amount <= 0:
        return None

    if DRY_RUN:
        log.info(f"[DRY RUN] Zou verkopen: {amount} {base_currency(symbol)}")
    else:
        exchange.create_market_sell_order(symbol, amount)
        log.info(f"SELL {amount} {base_currency(symbol)}")

    return amount


# ============================================================================
#  HOOFDLUS
# ============================================================================
def discover_watchlist(exchange):
    """Vraagt Kraken zelf welke EUR-paren er zijn en pakt de top WATCHLIST_SIZE
    op 24u-volume. Stablecoins (USDT/EUR, USDC/EUR...) slaan we over -- die
    bewegen per definitie amper, een breakout-strategie heeft daar niks te zoeken."""
    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        # Bewust breed gevangen: een hapering hier (network, rate limit, een
        # veld dat ontbreekt) mag nooit de hele cyclus laten crashen -- we
        # hebben een prima vaste lijst achter de hand.
        log.warning(f"Kon marktlijst/tickers niet ophalen ({e}) -- val terug op vaste lijst.")
        return list(FALLBACK_WATCHLIST)

    ranked = []
    for symbol, market in markets.items():
        if not market.get("active", True):
            continue
        if market.get("quote") != "EUR":
            continue
        if market.get("base") in STABLECOIN_BASES:
            continue
        if market.get("type") and market.get("type") != "spot":
            continue
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        volume_eur = ticker.get("quoteVolume") or 0
        if volume_eur > 0:
            ranked.append((volume_eur, symbol))

    if not ranked:
        log.warning("Geen enkel EUR-paar met volume gevonden -- val terug op vaste lijst.")
        return list(FALLBACK_WATCHLIST)

    ranked.sort(reverse=True)
    watchlist = [sym for _, sym in ranked[:WATCHLIST_SIZE]]
    log.info(f"Marktscan: {len(ranked)} actieve EUR-paren gevonden, top {len(watchlist)} op volume gekozen.")
    return watchlist


def find_adoptable_position(exchange):
    """Checkt of er al een saldo van een watchlist-coin op de rekening staat dat de bot
    nog niet als eigen positie kent -- bv. handmatig gekocht vóórdat de bot actief werd.
    Retourneert (symbol, hoeveelheid) van de eerste match boven het orderminimum, of None."""
    try:
        balance = exchange.fetch_balance().get("free", {})
    except ccxt.BaseError as e:
        log.warning(f"Kon balans niet ophalen voor adoptie-check ({e}).")
        return None

    for symbol in WATCHLIST:
        base = base_currency(symbol)
        amount = balance.get(base, 0) or 0
        if amount <= 0:
            continue
        try:
            price = float(exchange.fetch_ticker(symbol)["last"])
        except ccxt.BaseError:
            continue
        if amount * price >= MIN_ORDER_EUR:
            return symbol, amount

    return None


def compute_average_entry_price(exchange, symbol):
    """Herleidt een gewogen gemiddelde instapprijs uit de ECHTE trade-historie op Kraken
    (fetch_my_trades), i.p.v. te gokken. Simplificatie: telt alleen BUY-trades mee, geen
    FIFO-matching tegen eventuele sells -- voor het normale geval (een of enkele
    handmatige aankopen, geen sells) is dit nauwkeurig genoeg. Retourneert None als er
    geen trade-historie te vinden is (bv. via een andere app gekocht, of ouder dan wat
    de API teruggeeft) -- dan valt de aanroeper terug op de huidige prijs als schatting."""
    try:
        trades = exchange.fetch_my_trades(symbol, limit=200)
    except ccxt.BaseError as e:
        log.warning(f"Kon trade-historie niet ophalen voor {symbol} ({e}).")
        return None

    buys = [t for t in trades if t.get("side") == "buy" and t.get("price") and t.get("amount")]
    total_amount = sum(t["amount"] for t in buys)
    if total_amount <= 0:
        return None

    total_cost = sum(t["price"] * t["amount"] for t in buys)
    return total_cost / total_amount


def scan_for_entry(exchange, state):
    """Doorzoekt de watchlist en stapt in op de sterkste breakout, indien die er is."""
    adoptable = find_adoptable_position(exchange)
    if adoptable:
        symbol, amount = adoptable
        avg_price = compute_average_entry_price(exchange, symbol)
        try:
            current_price = float(exchange.fetch_ticker(symbol)["last"])
        except ccxt.BaseError:
            current_price = avg_price

        entry_price = avg_price if avg_price is not None else current_price
        source = "herleid uit je trade-historie" if avg_price is not None else "GESCHAT op de huidige prijs (geen trade-historie gevonden -- controleer dit)"
        log.info(
            f"Bestaand saldo gevonden: {amount} {base_currency(symbol)} -- geadopteerd als positie. "
            f"Instapprijs {source}: €{fmt_price(entry_price)}"
        )
        state.update({
            "in_position": True,
            "symbol": symbol,
            "entry_price": entry_price,
            "peak_price": max(entry_price, current_price),
            "current_price": current_price,
            "position_size": amount,
            "is_runner": False,
        })
        return state

    candidates = []
    snapshot = {}  # elke run vers -- paren die uit de watchlist vielen verdwijnen zo ook uit het dashboard
    checked_at = datetime.now(timezone.utc).isoformat()

    for symbol in WATCHLIST:
        try:
            last_closed = latest_closed_candle(exchange, symbol)
        except ccxt.BaseError as e:
            log.warning(f"{symbol}: kon data niet ophalen ({e}), sla over deze ronde.")
            continue

        ts = int(last_closed["ts"])
        price = float(last_closed["close"])

        if pd.isna(last_closed["donchian_high"]) or pd.isna(last_closed["atr"]) or pd.isna(last_closed["rsi"]):
            continue  # nog niet genoeg historie voor dit paar

        donchian_high = float(last_closed["donchian_high"])
        # Positief = prijs staat AL boven het kanaal (bullish, dicht bij of over de trigger).
        # Negatief = prijs moet nog stijgen. Dit is bewust andersom dan een letterlijke
        # "afstand tot", zodat positief/negatief meteen bullish/bearish betekent voor de UI.
        pct_vs_breakout = round((price / donchian_high - 1) * 100, 2) if donchian_high > 0 else None
        snapshot[symbol] = {
            "price": price,
            "donchian_high": donchian_high,
            "pct_vs_breakout": pct_vs_breakout,
            "checked_at": checked_at,
        }

        if ts == state["last_candle_ts"].get(symbol):
            continue  # geen nieuwe candle voor dit paar sinds vorige check

        state["last_candle_ts"][symbol] = ts

        breakout = price > donchian_high
        momentum_ok = (RSI_MIN_FOR_ENTRY <= 0) or (last_closed["rsi"] > RSI_MIN_FOR_ENTRY)

        log.info(
            f"Check {symbol:9s} | prijs €{fmt_price(price)} | Donchian-high €{fmt_price(donchian_high)} "
            f"| RSI {last_closed['rsi']:.1f} | ATR €{fmt_price(last_closed['atr'])}"
        )

        if breakout and momentum_ok:
            strength_in_atr = (price - donchian_high) / last_closed["atr"]
            candidates.append((strength_in_atr, symbol, price))

    state["watchlist_snapshot"] = snapshot

    if not candidates:
        return state

    candidates.sort(reverse=True)  # sterkste uitbraak eerst
    strength, symbol, price = candidates[0]
    if len(candidates) > 1:
        log.info(f"{len(candidates)} paren breken tegelijk uit -- kies {symbol} (sterkste, {strength:.1f}x ATR boven kanaal)")

    eur_balance = get_eur_balance(exchange)
    bought = place_buy(exchange, symbol, eur_balance, price)
    if bought:
        state.update({
            "in_position": True,
            "symbol": symbol,
            "entry_price": price,
            "peak_price": price,
            "current_price": price,
            "position_size": bought,
            "is_runner": False,
        })

    return state


def monitor_position(exchange, state):
    """Volgt de open positie en verkoopt zodra de tweetraps trailing-stop raakt."""
    symbol = state["symbol"]
    try:
        last_closed = latest_closed_candle(exchange, symbol)
    except ccxt.BaseError as e:
        log.warning(f"{symbol}: kon data niet ophalen ({e}), sla over deze ronde.")
        return state

    ts = int(last_closed["ts"])
    if ts == state["last_candle_ts"].get(symbol):
        return state

    if pd.isna(last_closed["atr"]):
        return state

    state["last_candle_ts"][symbol] = ts

    price = float(last_closed["close"])
    atr_now = float(last_closed["atr"])
    state["current_price"] = price
    state["current_atr"] = atr_now
    state["peak_price"] = max(state["peak_price"], price)

    # Eenmaal een runner, blijft een positie een runner (geen heen-en-weer
    # schakelen tussen smalle/brede stop als de winst rond de drempel hangt).
    profit_in_atr = (price - state["entry_price"]) / atr_now if atr_now > 0 else 0
    if profit_in_atr >= RUNNER_THRESHOLD_ATR:
        state["is_runner"] = True

    trail_mult = ATR_TRAIL_MULT_RUNNER if state["is_runner"] else ATR_TRAIL_MULT_INITIAL
    trail_stop = state["peak_price"] - trail_mult * atr_now

    log.info(
        f"In positie {symbol} | prijs €{fmt_price(price)} | piek €{fmt_price(state['peak_price'])} "
        f"| modus {'RUNNER (breed)' if state['is_runner'] else 'initieel (smal)'} "
        f"| trailing stop €{fmt_price(trail_stop)}"
    )

    if price < trail_stop:
        sell_amount = state["position_size"] if DRY_RUN else get_base_balance(exchange, symbol)
        sold = place_sell(exchange, symbol, sell_amount)
        if sold:
            pnl_pct = (price / state["entry_price"] - 1) * 100
            log.info(f"EXIT {symbol} via trailing stop | resultaat: {pnl_pct:+.1f}%")

            trade_history = state.get("trade_history", [])
            trade_history.append({
                "symbol": symbol,
                "entry_price": state["entry_price"],
                "exit_price": price,
                "pnl_pct": round(pnl_pct, 2),
                "was_runner": state["is_runner"],
                "exit_time": datetime.now(timezone.utc).isoformat(),
            })
            state["trade_history"] = trade_history[-50:]  # laatste 50 volstaat, houdt het bestand klein

            state.update({
                "in_position": False,
                "symbol": None,
                "entry_price": None,
                "peak_price": None,
                "current_price": None,
                "current_atr": None,
                "position_size": None,
                "is_runner": False,
            })

    return state


def run_cycle(exchange, state):
    if state["in_position"]:
        state = monitor_position(exchange, state)
    else:
        state = scan_for_entry(exchange, state)
    state["config"] = {"watchlist": WATCHLIST, "runner_threshold_atr": RUNNER_THRESHOLD_ATR}
    state["dry_run"] = DRY_RUN
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()  # de ECHTE laatste-draai-tijd,
                                                                     # niet wanneer het dashboard 'm ophaalt
    save_state(state)
    return state


def write_status_file(state):
    """Schrijft een leesbare README.md -- zichtbaar op de GitHub-repopagina zelf,
    dus ook prima te bekijken vanaf je telefoon zonder ergens in te loggen."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "DRY RUN (geen echte orders)" if DRY_RUN else "LIVE (echte orders!)"

    if state["in_position"]:
        body = (
            f"**Status:** In positie ({mode})\n\n"
            f"| | |\n|---|---|\n"
            f"| Paar | `{state['symbol']}` |\n"
            f"| Instapprijs | €{fmt_price(state['entry_price'])} |\n"
            f"| Piek sinds instap | €{fmt_price(state['peak_price'])} |\n"
            f"| Modus | {'RUNNER (brede stop)' if state['is_runner'] else 'initieel (smalle stop)'} |\n"
        )
    else:
        body = (
            f"**Status:** Scannen, geen open positie ({mode})\n\n"
            f"Watchlist: top {len(WATCHLIST)} EUR-paren op 24u-volume (ververst elke run).\n"
        )

    content = (
        f"# Kraken multi-coin momentum bot\n\n"
        f"Automatische breakout-bot, scant dynamisch de top {len(WATCHLIST)} EUR-paren op Kraken "
        f"(op volume), draait elke ~15 min via GitHub Actions. Dit bestand wordt door de bot zelf "
        f"overschreven bij elke run. Zie het dashboard voor het volledige overzicht.\n\n"
        f"---\n\n"
        f"**Laatste check:** {now}\n\n"
        f"{body}\n"
        f"---\n"
        f"*Automatisch bijgewerkt door de bot bij elke run. Bewerk dit bestand niet handmatig.*\n"
    )
    STATUS_FILE.write_text(content)


def main():
    global WATCHLIST
    exchange = build_exchange()
    state = load_state()

    WATCHLIST = discover_watchlist(exchange)
    # State opschonen voor paren die niet meer in de watchlist zitten, anders
    # blijven ze onterecht in het dashboard staan met stokoude cijfers.
    state["last_candle_ts"] = {s: t for s, t in state.get("last_candle_ts", {}).items() if s in WATCHLIST}

    mode = "DRY RUN (geen echte orders)" if DRY_RUN else "LIVE (echte orders!)"
    log.info(
        f"Cyclus gestart | {mode} | {len(WATCHLIST)} paren | {TIMEFRAME} | "
        f"breakout={BREAKOUT_LOOKBACK} candles | trail={ATR_TRAIL_MULT_INITIAL}x->{ATR_TRAIL_MULT_RUNNER}xATR "
        f"(runner vanaf {RUNNER_THRESHOLD_ATR}x ATR winst) | RSI-min={RSI_MIN_FOR_ENTRY}"
    )

    try:
        state = run_cycle(exchange, state)
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        # Tijdelijk gedoe (Kraken hapert, rate limit, etc.) -- de volgende
        # geplande run (over ~15 min) probeert het gewoon opnieuw. Geen
        # reden om de workflow als "failed" te laten zien in GitHub.
        log.warning(f"{type(e).__name__}: {e}. Volgende cyclus probeert opnieuw.")
    # Onverwachte fouten (bug, etc.) laten we WEL doorbubbelen, zodat de
    # workflow rood kleurt in de Actions-tab -- dat wil je zien.

    write_status_file(state)
    log.info("Cyclus klaar.")


if __name__ == "__main__":
    main()
