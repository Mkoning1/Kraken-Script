"""Risico-agent: heeft altijd het laatste woord, apart voor het echte geld en het schaduwgeld.

- Inzet: zoveel dat de noodstop precies risk_per_trade_pct van je vermogen kost,
  meer bij een hoge score (tot 1,5x), minder bij een lage (tot 0,5x).
- Grenzen: max grootte per positie, max aantal posities, max totaal belegd, beschikbaar geld.
  Nooit geleend geld.
- Na een verliestrade: een paar candles afkoelen voor die munt.
- Verlies vandaag boven max_daily_loss_pct: vandaag niets nieuws.
- Vermogen max_drawdown_pct onder de hoogste stand: NOODSTOP, alles verkopen, stoppen.
"""
from datetime import datetime, timezone

from .fmt import eur, px


class RiskAgent:
    label = "Risico-agent"

    def __init__(self, profile, min_order_eur):
        self.p = profile
        self.min_order = min_order_eur

    def update(self, book, equity, now_ts, trading_enabled=True):
        today = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d")
        if book["day"].get("date") != today:
            book["day"] = {"date": today, "start_equity": equity}
        book["peak_equity"] = max(book["peak_equity"], equity)
        start = book["day"]["start_equity"] or equity
        daily_loss = max(0.0, (start - equity) / start * 100) if start else 0.0
        drawdown = max(0.0, (book["peak_equity"] - equity) / book["peak_equity"] * 100) if book["peak_equity"] else 0.0
        trigger = False
        if not book["halted"] and drawdown >= self.p["max_drawdown_pct"]:
            book["halted"] = True
            book["halt_reason"] = f"Vermogen {drawdown:.1f}% onder de hoogste stand (grens {self.p['max_drawdown_pct']}%)"
            trigger = True
        return {"daily_loss_pct": round(daily_loss, 2), "drawdown_pct": round(drawdown, 2),
                "halted": book["halted"], "halt_reason": book.get("halt_reason"),
                "daily_limit_hit": daily_loss >= self.p["max_daily_loss_pct"],
                "trading_enabled": trading_enabled}, trigger

    def approve_entry(self, book, status, equity, exposure, market, price, stop, available_cash, score, candle_ts):
        """Geeft (ok, hoeveelheid, risico in euro, uitleg)."""
        if status["halted"]:
            return False, 0, 0, "Noodstop actief"
        if not status["trading_enabled"]:
            return False, 0, 0, "Handelen staat uit in config.json"
        if status["daily_limit_hit"]:
            return False, 0, 0, f"Daglimiet bereikt ({status['daily_loss_pct']:.1f}% verlies vandaag)"
        if len(book["positions"]) >= self.p["max_open_positions"]:
            return False, 0, 0, f"Maximum van {self.p['max_open_positions']} posities bereikt"
        if book["cooldown"].get(market, 0) > candle_ts:
            return False, 0, 0, "Afkoelperiode na een verliestrade in deze munt"
        if stop <= 0 or stop >= price:
            return False, 0, 0, "Geen geldige stop-loss te berekenen"

        risk_per_unit = price - stop
        scale = max(0.5, min(1.5, score))
        wanted_risk = equity * self.p["risk_per_trade_pct"] / 100 * scale
        qty = wanted_risk / risk_per_unit
        caps = {
            "max positiegrootte": equity * self.p["max_position_pct"] / 100,
            "max totaal belegd": equity * self.p["max_exposure_pct"] / 100 - exposure,
            "beschikbaar geld": available_cash * 0.99,
        }
        capped_by = None
        for why, cap in caps.items():
            if qty * price > cap:
                qty, capped_by = max(cap, 0) / price, why
        value = qty * price
        if value < self.min_order:
            return False, 0, 0, f"Te weinig ruimte voor een order ({eur(value)}, grens: {capped_by or 'minimum'})"
        risk_eur = qty * risk_per_unit
        text = (f"Inzet {eur(value)} ({value / equity * 100:.0f}% van het vermogen"
                + (f", begrensd door {capped_by}" if capped_by else "") + "). "
                f"Noodstop {px(stop)} ({risk_per_unit / price * 100:.1f}% lager): maximaal verlies ca. {eur(risk_eur)} "
                f"({risk_eur / equity * 100:.1f}% van het vermogen) plus kosten.")
        return True, qty, risk_eur, text
