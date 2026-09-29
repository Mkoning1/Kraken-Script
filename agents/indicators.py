"""Technische indicatoren, geschreven zonder externe libraries."""
import math


def sma(values, period):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values, period):
    """Exponentieel voortschrijdend gemiddelde: recente candles tellen zwaarder."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes, period=14):
    """Relative Strength Index volgens Wilder (0-100). Laag = hard gedaald."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


def _rsi(avg_g, avg_l):
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(highs, lows, closes, period=14):
    """Average True Range: hoeveel de koers gemiddeld beweegt per candle."""
    n = len(closes)
    out = [None] * n
    if n < period:
        return out
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    a = sum(trs[:period]) / period
    out[period - 1] = a
    for i in range(period, n):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """MACD-lijn en signaallijn. Kruist de MACD omhoog door de signaallijn, dan versnelt de koers."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    start = next(i for i, v in enumerate(line) if v is not None)
    sig_part = ema(line[start:], signal)
    sig = [None] * start + sig_part
    return line, sig


def bollinger(closes, period=20, mult=2.0):
    """Bollinger-banden: gemiddelde plus/min 2 standaarddeviaties. Bandbreedte = hoe onrustig de markt is."""
    mid = sma(closes, period)
    upper, lower, width = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        upper[i], lower[i] = m + mult * sd, m - mult * sd
        width[i] = (upper[i] - lower[i]) / m if m else None
    return mid, upper, lower, width


def snapshot(candles, cfg):
    """Alle indicatoren voor de laatste afgesloten candle (en waar nodig de candle ervoor)."""
    if len(candles) < 260:
        raise ValueError(f"te weinig candles ({len(candles)})")
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    vols = [c["volume"] for c in candles]

    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes, 14)
    a = atr(highs, lows, closes, 14)
    m_line, m_sig = macd(closes)
    bb_mid, bb_up, bb_low, bb_w = bollinger(closes, 20, 2.0)
    vol_avg = sma(vols, 20)

    lookback = cfg["agents"].get("breakout", {}).get("lookback", 24)
    sq_look = cfg["agents"].get("squeeze", {}).get("squeeze_lookback", 120)
    widths = [w for w in bb_w[-sq_look - 1:-1] if w is not None]
    prev_w = bb_w[-2]
    squeeze_rank = sum(1 for w in widths if w < prev_w) / len(widths) * 100 if widths else 50

    return {
        "close": closes[-1], "prev_close": closes[-2],
        "ema20": e20[-1], "ema50": e50[-1], "ema200": e200[-1],
        "rsi": r[-1], "atr": a[-1],
        "macd": m_line[-1], "macd_signal": m_sig[-1],
        "prev_macd": m_line[-2], "prev_macd_signal": m_sig[-2],
        "bb_mid": bb_mid[-1], "bb_upper": bb_up[-1], "bb_lower": bb_low[-1],
        "squeeze_rank": squeeze_rank,
        "prev_high": max(highs[-(lookback + 1):-1]),
        "volume": vols[-1], "volume_avg": vol_avg[-2] or vol_avg[-1],
    }


def trend_snapshot(candles):
    """Grotere tijdschaal (uur): alleen de trend."""
    closes = [c["close"] for c in candles]
    if len(closes) < 210:
        raise ValueError(f"te weinig uurcandles ({len(closes)})")
    return {"close": closes[-1], "ema50": ema(closes, 50)[-1], "ema200": ema(closes, 200)[-1]}


def rsi_sma(closes, period=14):
    """RSI met gewoon gemiddelde (zoals in je oorspronkelijke bot)."""
    out = [None] * len(closes)
    for i in range(period, len(closes)):
        diffs = [closes[j] - closes[j - 1] for j in range(i - period + 1, i + 1)]
        g = sum(max(d, 0) for d in diffs) / period
        l = sum(max(-d, 0) for d in diffs) / period
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr_sma(highs, lows, closes, period=14):
    """ATR met gewoon gemiddelde (zoals in je oorspronkelijke bot)."""
    trs = [highs[0] - lows[0]] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                                  for i in range(1, len(closes))]
    return sma(trs, period)


def snapshot_4h(candles, lookback=55):
    """4-uursbeeld voor de Trend-4u-agent: Donchian-kanaal, RSI en ATR van de laatste afgesloten candle."""
    if len(candles) < lookback + 16:
        raise ValueError(f"te weinig 4-uurscandles ({len(candles)})")
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    return {
        "ts": candles[-1]["ts"],
        "close": closes[-1],
        "donchian_high": max(highs[-lookback - 1:-1]),
        "rsi": rsi_sma(closes, 14)[-1],
        "atr": atr_sma(highs, lows, closes, 14)[-1],
    }
