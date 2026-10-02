"""Markt-agent en strategie-agents.

De markt-agent bepaalt of de hele cryptomarkt stijgt, daalt of zijwaarts gaat (op basis van BTC).
Elke strategie-agent kijkt op zijn eigen manier naar één munt en geeft een advies:
kopen, verkopen of afwachten, met een sterkte (0-1) en een reden. Ze handelen niet zelf.
"""
from dataclasses import dataclass, asdict


@dataclass
class Signal:
    action: str                 # "buy", "sell" of "hold"
    strength: float             # 0.0 - 1.0
    reason: str
    stop_atr: float = 2.0       # stop-loss afstand in ATR's
    trailing: bool = False      # stop schuift mee omhoog
    max_candles: int = 0        # 0 = geen tijdslimiet
    exit_style: str = "r_trail" # "r_trail" (korte termijn) of "two_stage" (Trend-4u)

    def to_dict(self):
        return {"action": self.action, "strength": round(self.strength, 2), "reason": self.reason}


class MarketRegimeAgent:
    label = "Markt-agent"

    def assess(self, htf):
        c, e50, e200 = htf["close"], htf["ema50"], htf["ema200"]
        if c > e200 and e50 > e200:
            return "stijgend", "BTC staat boven zijn gemiddelde van ruim een maand en de trend wijst omhoog"
        if c < e200 and e50 < e200:
            return "dalend", "BTC staat onder zijn gemiddelde van ruim een maand en de trend wijst omlaag"
        return "neutraal", "BTC heeft geen duidelijke richting"


def _htf_up(htf):
    return htf["close"] > htf["ema200"]


class MeanReversionAgent:
    horizon = "kort"
    """Koopt paniekdalingen in een munt die op uurbasis stijgt, verkoopt zodra de koers terug is bij het gemiddelde."""
    name, label = "mean_reversion", "Dip-koper"

    def __init__(self, rsi_buy=28, stop_atr=1.8, max_candles=24, **_):
        self.rsi_buy, self.stop_atr, self.max_candles = rsi_buy, stop_atr, max_candles

    def analyse(self, ctx):
        i, htf = ctx["sig"], ctx["trend"]
        if _htf_up(htf) and i["rsi"] < self.rsi_buy and i["close"] < i["bb_lower"]:
            strength = min(1.0, 0.6 + (self.rsi_buy - i["rsi"]) / 25)
            return Signal("buy", strength,
                          f"Paniekdaling: RSI {i['rsi']:.0f} en koers onder de onderste Bollinger-band, 4-uurstrend omhoog",
                          self.stop_atr, False, self.max_candles)
        if i["close"] >= i["bb_mid"]:
            return Signal("sell", 0.7, "Koers terug bij het gemiddelde: dip is hersteld")
        if not _htf_up(htf):
            return Signal("sell", 0.6, "4-uurstrend is omgeslagen naar dalend")
        return Signal("hold", 0.0, f"Geen paniekdaling (RSI {i['rsi']:.0f})")


class BreakoutAgent:
    horizon = "kort"
    """Koopt als de koers met volume door het hoogste punt van de afgelopen periode breekt."""
    name, label = "breakout", "Uitbraak-agent"

    def __init__(self, lookback=24, volume_mult=1.2, stop_atr=2.5, **_):
        self.lookback, self.volume_mult, self.stop_atr = lookback, volume_mult, stop_atr

    def analyse(self, ctx):
        i, htf = ctx["sig"], ctx["trend"]
        vol_ratio = i["volume"] / i["volume_avg"] if i["volume_avg"] else 0
        if _htf_up(htf) and i["close"] > i["prev_high"] and vol_ratio >= self.volume_mult and i["ema20"] > i["ema50"]:
            strength = min(0.95, 0.7 + (vol_ratio - self.volume_mult) * 0.1)
            return Signal("buy", strength,
                          f"Uitbraak boven het hoogste punt van de laatste {self.lookback} uur met {vol_ratio:.1f}x normaal volume",
                          self.stop_atr, True)
        if not _htf_up(htf):
            return Signal("sell", 0.9, "Koers onder het 4-uursgemiddelde van ruim een maand: dalende markt")
        if i["close"] < i["ema50"]:
            return Signal("sell", 0.6, "Koers onder het 50-uursgemiddelde: uitbraak mislukt")
        return Signal("hold", 0.0, "Geen uitbraak")


class MomentumAgent:
    horizon = "kort"
    """Springt op een versnelling: MACD kruist omhoog, met extra volume, boven het gemiddelde."""
    name, label = "momentum", "Momentum-agent"

    def __init__(self, volume_mult=1.5, stop_atr=2.0, **_):
        self.volume_mult, self.stop_atr = volume_mult, stop_atr

    def analyse(self, ctx):
        i, htf = ctx["sig"], ctx["trend"]
        crossed_up = i["prev_macd"] <= i["prev_macd_signal"] and i["macd"] > i["macd_signal"]
        crossed_down = i["prev_macd"] >= i["prev_macd_signal"] and i["macd"] < i["macd_signal"]
        vol_ratio = i["volume"] / i["volume_avg"] if i["volume_avg"] else 0
        if crossed_up and vol_ratio >= self.volume_mult and i["close"] > i["ema50"] and htf["close"] > htf["ema50"]:
            strength = min(0.9, 0.65 + (vol_ratio - self.volume_mult) * 0.08)
            return Signal("buy", strength, f"Versnelling: MACD kruist omhoog met {vol_ratio:.1f}x volume",
                          self.stop_atr, True)
        if crossed_down:
            return Signal("sell", 0.65, "MACD kruist omlaag: vaart gaat eruit")
        return Signal("hold", 0.0, "Geen versnelling")


class SqueezeAgent:
    horizon = "kort"
    """Wacht tot de markt heel rustig is (smalle banden) en koopt de uitbraak omhoog daarna."""
    name, label = "squeeze", "Squeeze-agent"

    def __init__(self, squeeze_pct=10, stop_atr=2.0, **_):
        self.squeeze_pct, self.stop_atr = squeeze_pct, stop_atr

    def analyse(self, ctx):
        i, htf = ctx["sig"], ctx["trend"]
        was_squeezed = i["squeeze_rank"] <= self.squeeze_pct
        if was_squeezed and i["close"] > i["bb_upper"] and htf["close"] > htf["ema50"]:
            return Signal("buy", 0.8, "Na een zeer rustige periode breekt de koers boven de bovenste band",
                          self.stop_atr, True)
        if i["close"] < i["ema50"]:
            return Signal("sell", 0.6, "Koers terug onder het 50-uursgemiddelde: uitbraak mislukt")
        state = "rustig, wacht op uitbraak" if was_squeezed else "niet rustig genoeg"
        return Signal("hold", 0.0, f"Markt {state}")


class Trend4hAgent:
    """Jouw oorspronkelijke bot als agent: Donchian-uitbraak op 4-uurscandles.

    Kopen: de 4-uursslotkoers breekt boven het hoogste punt van de vorige 55 candles (ca. 9 dagen)
    en de RSI staat boven 50. Hoe verder boven het kanaal (in ATR's), hoe sterker het advies.
    Verkopen: alleen via de tweetraps trailing stop (2 ATR krap, 6 ATR ruim zodra de winst 3 ATR is).
    """
    name, label, horizon = "trend4h", "Trend-4u", "lang"

    def __init__(self, lookback=55, rsi_min=50, initial_atr=2.0, **_):
        self.lookback, self.rsi_min, self.initial_atr = lookback, rsi_min, initial_atr

    def analyse(self, ctx):
        h4 = ctx.get("h4")
        if not h4:
            return Signal("hold", 0.0, "Wacht op de volgende gesloten 4-uurscandle")
        above = (h4["close"] - h4["donchian_high"]) / h4["atr"] if h4["atr"] else 0
        if h4["close"] > h4["donchian_high"] and h4["rsi"] > self.rsi_min:
            strength = min(1.0, 0.7 + above * 0.15)
            return Signal("buy", strength,
                          f"4-uursuitbraak: slot {above:.1f} ATR boven het hoogste punt van {self.lookback} candles, RSI {h4['rsi']:.0f}",
                          self.initial_atr, True, 0, "two_stage")
        pct = (h4["close"] / h4["donchian_high"] - 1) * 100
        return Signal("hold", 0.0, f"Nog {abs(pct):.1f}% onder het uitbraakniveau" if pct < 0 else f"Boven het kanaal maar RSI {h4['rsi']:.0f} te laag")


AGENT_CLASSES = {c.name: c for c in (Trend4hAgent, MeanReversionAgent, BreakoutAgent, MomentumAgent, SqueezeAgent)}


def build_agents(agent_cfg):
    agents = []
    for n, opts in agent_cfg.items():
        if n in AGENT_CLASSES and opts.get("enabled", True):
            a = AGENT_CLASSES[n](**opts)
            a.use_regime = opts.get("use_regime", True)
            agents.append(a)
    return agents
