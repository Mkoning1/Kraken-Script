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
  - Zolang er geen positie open is, checkt de bot elke cyclus ALLE paren in
    WATCHLIST op een Donchian-breakout.
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
DRY_RUN = True   # True = alleen loggen wat er zou gebeuren, GEEN echte orders.
                 # Pas op False zetten als je de logica een tijdje hebt gevolgd.

# ============================================================================
#  API-KEYS -- komen uit GitHub Secrets, staan NERGENS in dit bestand
# ============================================================================
API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")

# Liquide, al lang op Kraken genoteerde paren, nu aangevuld met memecoins.
# DOGE/SHIB/PEPE heb ik gecontroleerd: bestaan echt als EUR-paar op Kraken.
# Wil je er meer bij (WIF, BONK, FLOKI...): check zelf even op kraken.com of
# de EUR-variant bestaat. Bestaat een paar niet of wordt het gedelist, dan
# logt de bot een waarschuwing en slaat 'm gewoon over -- geen crash.
#
# Let op bij memecoins t.o.v. de rest van de lijst: dunnere orderboeken dan
# ADA/SOL/DOT, dus een market sell tijdens een scherpe crash kan slechter
# vullen dan de laatste prijs die de bot zag. De ATR-logica schaalt vanzelf
# mee met hun hogere volatiliteit, dat is geen probleem -- de fill zelf kan
# wel rommeliger zijn dan bij de grotere paren.
WATCHLIST  = [
    "ADA/EUR", "SOL/EUR", "DOT/EUR", "LINK/EUR", "AVAX/EUR",
    "DOGE/EUR", "SHIB/EUR", "PEPE/EUR",
]
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
def scan_for_entry(exchange, state):
    """Doorzoekt de watchlist en stapt in op de sterkste breakout, indien die er is."""
    candidates = []
    snapshot = dict(state.get("watchlist_snapshot", {}))  # vorige snapshot als basis, per paar bijwerken
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
        snapshot[symbol] = {
            "price": price,
            "donchian_high": donchian_high,
            "pct_to_breakout": round((donchian_high / price - 1) * 100, 2) if price > 0 else None,
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
            f"Watchlist: {', '.join(f'`{s}`' for s in WATCHLIST)}\n"
        )

    content = (
        f"# Kraken multi-coin momentum bot\n\n"
        f"Automatische breakout-bot, scant {len(WATCHLIST)} paren, draait elke ~15 min via "
        f"GitHub Actions. Dit bestand wordt door de bot zelf overschreven bij elke run.\n\n"
        f"---\n\n"
        f"**Laatste check:** {now}\n\n"
        f"{body}\n"
        f"---\n"
        f"*Automatisch bijgewerkt door de bot bij elke run. Bewerk dit bestand niet handmatig.*\n"
    )
    STATUS_FILE.write_text(content)


def main():
    exchange = build_exchange()
    state = load_state()
    mode = "DRY RUN (geen echte orders)" if DRY_RUN else "LIVE (echte orders!)"
    log.info(
        f"Cyclus gestart | {mode} | watchlist: {', '.join(WATCHLIST)} | {TIMEFRAME} | "
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
