"""Uitvoer-agents: voeren orders uit en bewaken elke positie.

Twee soorten uitstap:
  r_trail (korte-termijnagents): harde stop-loss; bij 1R winst naar instapprijs plus kosten (breakeven);
      vanaf 1,5R schuift de stop mee onder de hoogste slotkoers; de dip-koper heeft een tijdslimiet.
  two_stage (Trend-4u, jouw bot): op elke gesloten 4-uurscandle een trailing stop 2 ATR onder de piek;
      zodra de winst 3 ATR is wordt het een 'runner' en gaat de stop naar 6 ATR onder de piek.
      Sluit een 4-uurscandle onder de stop, dan verkopen. Daarnaast een noodstop 1 ATR onder die stop
      die bij echt geld als stop-loss order op Kraken staat, zodat je ook beschermd bent als GitHub hapert.

PaperExecutionAgent simuleert (schaduwgeld). LiveExecutionAgent handelt met echt geld via Kraken.
"""
import time

from .fmt import eur, px


# ----------------------------------------------------------------------------------------------
# Gedeelde logica
# ----------------------------------------------------------------------------------------------
def plan_stops(signal, price, ctx, exits, agent_cfg):
    """Stop-loss voor een nieuwe positie. Geeft (stop, noodstop, extra velden)."""
    if signal.exit_style == "two_stage":
        atr4 = ctx["h4"]["atr"]
        stop = price - agent_cfg.get("initial_atr", 2.0) * atr4
        hard = stop - exits["catastrophe_atr"] * atr4
        extra = {"atr": atr4, "peak": price, "is_runner": False, "last_4h_ts": ctx["h4"]["ts"],
                 "initial_atr": agent_cfg.get("initial_atr", 2.0), "runner_atr": agent_cfg.get("runner_atr", 6.0),
                 "runner_threshold_atr": agent_cfg.get("runner_threshold_atr", 3.0)}
        return stop, hard, extra
    atr1 = ctx["sig"]["atr"]
    stop = min(price - signal.stop_atr * atr1, price * (1 - exits["min_stop_pct"] / 100))
    return stop, stop, {}


def exchange_stop(pos):
    return pos["hard_stop"] if pos["exit_style"] == "two_stage" else pos["stop"]


def advance(pos, new_candles, h4_list, exits, fee, slip, tf_sec, intrabar):
    """Loop nieuwe candles af, verplaats stops en geef (prijs, tijd, reden) terug als de positie dicht moet.
    intrabar=True: ook controleren of een stop binnen een candle geraakt is (papier en backtest).
    Bij echt geld doet Kraken dat zelf met de stop-loss order.
    h4_list: alle gesloten 4-uurscandles met hun ATR; gemiste candles worden alsnog verwerkt."""
    for c in new_candles:
        if c["ts"] <= pos["last_checked_ts"]:
            continue
        close_ts = c["ts"] + tf_sec
        hs = exchange_stop(pos)
        if intrabar and c["low"] <= hs:
            kind = "Noodstop" if pos["exit_style"] == "two_stage" else _stop_kind(pos, fee, slip)
            return min(c["open"], hs), close_ts, f"{kind} geraakt op {px(hs)}"
        pos["last_checked_ts"] = c["ts"]
        pos["candles_held"] += 1
        if pos["exit_style"] == "r_trail":
            pos["highest_close"] = max(pos["highest_close"], c["close"])
            progress = (c["close"] - pos["entry_price"]) / pos["r_unit"] if pos["r_unit"] else 0
            breakeven = pos["entry_price"] * (1 + 2 * fee + slip)
            if progress >= exits["breakeven_at_r"] and pos["stop"] < breakeven:
                pos["stop"] = breakeven
            if pos["trailing"] and progress >= exits["trail_after_r"]:
                pos["stop"] = max(pos["stop"], pos["highest_close"] - pos["trail_distance"])
            pos["hard_stop"] = pos["stop"]
            if pos["max_candles"] and pos["candles_held"] >= pos["max_candles"]:
                return c["close"], close_ts, f"Tijdslimiet: na {pos['max_candles']} candles geen herstel"

    if pos["exit_style"] == "two_stage" and h4_list:
        for h4 in h4_list:
            if h4["ts"] <= pos["last_4h_ts"] or not h4.get("atr"):
                continue
            pos["last_4h_ts"] = h4["ts"]
            price, atr = h4["close"], h4["atr"]
            pos["atr"] = atr
            pos["peak"] = max(pos["peak"], price)
            if (price - pos["entry_price"]) / atr >= pos["runner_threshold_atr"]:
                pos["is_runner"] = True
            mult = pos["runner_atr"] if pos["is_runner"] else pos["initial_atr"]
            pos["stop"] = pos["peak"] - mult * atr
            pos["hard_stop"] = pos["stop"] - exits["catastrophe_atr"] * atr
            if price < pos["stop"]:
                mode = "runner, 6 ATR" if pos["is_runner"] else "krap, 2 ATR"
                return price, h4["ts"] + 14400, f"4-uursslot onder de trailing stop ({mode}) op {px(pos['stop'])}"
    return None


def _stop_kind(pos, fee, slip):
    if pos["stop"] <= pos["entry_price"]:
        return "Stop-loss"
    if pos["stop"] > pos["entry_price"] * (1 + 2 * fee + slip) * 1.001:
        return "Meeschuivende stop (winst veiliggesteld)"
    return "Breakeven-stop"


def new_position(book, market, agent, signal, qty, fill, cost, fee, stop, hard, extra, risk_eur, candle_ts, tf_sec, reason, score, plan):
    extra = dict(extra or {})
    position_tf_sec = int(extra.pop("_tf_sec", tf_sec))
    cooldown_sec = int(extra.pop("_cooldown_sec", 0))
    pos = {
        "market": market, "agent": agent, "book": book["name"], "qty": qty,
        "entry_price": fill, "entry_cost": cost + fee, "entry_ts": candle_ts + position_tf_sec,
        "exit_style": signal.exit_style, "stop": stop, "initial_stop": stop, "hard_stop": hard,
        "risk_eur": risk_eur, "r_unit": fill - stop if fill > stop else fill * 0.02,
        "trailing": signal.trailing, "trail_distance": fill - stop, "max_candles": signal.max_candles,
        "candles_held": 0, "highest_close": fill, "last_checked_ts": candle_ts,
        "timeframe_sec": position_tf_sec, "cooldown_sec": cooldown_sec,
        "entry_reason": reason, "score": round(score, 2), "plan": plan,
    }
    pos.update(extra)
    book["positions"][market] = pos
    return pos


def record_exit(book, pos, fill, proceeds_net, fee, ts, reason, learner, cooldown_sec):
    book["positions"].pop(pos["market"], None)
    book["cash"] += proceeds_net
    book["fees_paid"] += fee
    pnl = proceeds_net - pos["entry_cost"]
    trade = {
        "market": pos["market"], "agent": pos["agent"], "book": book["name"], "qty": pos["qty"],
        "entry_price": pos["entry_price"], "exit_price": fill, "entry_ts": pos["entry_ts"], "exit_ts": ts,
        "entry_cost": round(pos["entry_cost"], 4), "pnl": pnl, "pnl_pct": pnl / pos["entry_cost"] * 100 if pos["entry_cost"] else 0,
        "r": pnl / pos["risk_eur"] if pos.get("risk_eur") else 0.0,
        "was_runner": pos.get("is_runner", False),
        "entry_reason": pos["entry_reason"], "exit_reason": reason,
    }
    book["trades"].append(trade)
    learner.record(trade)
    if pnl < 0:
        book["cooldown"][pos["market"]] = ts + int(pos.get("cooldown_sec") or cooldown_sec)
    return trade


# ----------------------------------------------------------------------------------------------
# Papier (schaduwgeld)
# ----------------------------------------------------------------------------------------------
class PaperExecutionAgent:
    label = "Uitvoer-agent (schaduw)"

    def __init__(self, cfg, tf_minutes, cooldown_candles):
        self.fee = cfg["fee_pct"] / 100
        self.slip = cfg["slippage_pct"] / 100
        self.exits = cfg["exits"]
        self.tf_sec = tf_minutes * 60
        self.cooldown_sec = cooldown_candles * self.tf_sec

    def buy(self, book, market, agent, signal, qty, price, stop, hard, extra, risk_eur, candle_ts, reason, score, plan):
        fill = price * (1 + self.slip)
        cost = qty * fill
        fee = cost * self.fee
        book["cash"] -= cost + fee
        book["fees_paid"] += fee
        new_position(book, market, agent, signal, qty, fill, cost, fee, stop, hard, extra, risk_eur,
                     candle_ts, self.tf_sec, reason, score, plan)
        return {"ok": True, "text": f"Papieren aankoop tegen {px(fill)}, kosten {eur(fee)}"}

    def sell(self, book, market, price, ts, reason, learner):
        pos = book["positions"][market]
        fill = price * (1 - self.slip)
        proceeds = pos["qty"] * fill
        fee = proceeds * self.fee
        return record_exit(book, pos, fill, proceeds - fee, fee, ts, reason, learner, self.cooldown_sec)

    def manage(self, book, market, new_candles, h4_list, learner, last_price):
        pos = book["positions"].get(market)
        if not pos:
            return None
        hit = advance(pos, new_candles, h4_list, self.exits, self.fee, self.slip,
                      int(pos.get("timeframe_sec") or self.tf_sec), intrabar=True)
        return self.sell(book, market, hit[0], hit[1], hit[2], learner) if hit else None


# ----------------------------------------------------------------------------------------------
# Echt geld via Kraken (ccxt)
# ----------------------------------------------------------------------------------------------
class LiveExecutionAgent:
    label = "Uitvoer-agent (echt geld)"

    def __init__(self, exchange, cfg, tf_minutes, cooldown_candles, validate_only, errors):
        self.ex = exchange
        self.fee = cfg["fee_pct"] / 100
        self.slip = cfg["slippage_pct"] / 100
        self.exits = cfg["exits"]
        self.tf_sec = tf_minutes * 60
        self.cooldown_sec = cooldown_candles * self.tf_sec
        self.validate = validate_only
        self.errors = errors
        self._bal = None
        self.now = int(time.time())

    # --- hulpfuncties ---------------------------------------------------------------------
    def balance(self, refresh=False):
        if self._bal is None or refresh:
            self._bal = self.ex.fetch_balance()
        return self._bal

    def free_eur(self):
        return float(self.balance()["free"].get("EUR", 0) or 0)

    def _base(self, market):
        return self.ex.market(market)["base"]

    def _params(self, extra=None):
        p = dict(extra or {})
        if self.validate:
            p["validate"] = "true"
        return p

    def _wait_fill(self, order_id, market):
        o = None
        for _ in range(8):
            o = self.ex.fetch_order(order_id, market)
            if o.get("status") == "closed" or (o.get("remaining") == 0 and o.get("filled")):
                break
            time.sleep(1.5)
        filled = float(o.get("filled") or 0)
        avg = float(o.get("average") or o.get("price") or 0)
        cost = float(o.get("cost") or filled * avg)
        fee_eur, fee_base = 0.0, 0.0
        fee = o.get("fee") or {}
        if fee.get("cost") is not None:
            if fee.get("currency") == "EUR":
                fee_eur = float(fee["cost"])
            else:
                fee_base = float(fee["cost"])
        else:
            fee_eur = cost * self.fee
        return filled, avg, cost, fee_eur, fee_base, o

    def _place_stop(self, pos):
        market = pos["market"]
        price = float(self.ex.price_to_precision(market, exchange_stop(pos)))
        base_total = float(self.balance(refresh=True)["free"].get(self._base(market), 0) or 0)
        amount = float(self.ex.amount_to_precision(market, min(pos["qty"], base_total) if base_total else pos["qty"]))
        if amount <= 0:
            raise RuntimeError(f"geen {self._base(market)} vrij om een stop-loss voor te plaatsen")
        o = self.ex.create_order(market, "market", "sell", amount, None, self._params({"stopLossPrice": price}))
        pos["exchange_stop_price"] = price
        if self.validate:
            pos["stop_validated"] = True
            return
        pos["stop_order_id"] = o.get("id")

    def _cancel_stop(self, pos):
        """Stop-order weghalen. Geeft de order terug als die intussen al uitgevoerd bleek."""
        oid = pos.get("stop_order_id")
        if not oid:
            return None
        try:
            self.ex.cancel_order(oid, pos["market"])
            pos["stop_order_id"] = None
            return None
        except Exception:
            o = self.ex.fetch_order(oid, pos["market"])
            if o.get("status") == "closed":
                pos["stop_order_id"] = None
                return o
            if o.get("status") in ("canceled", "expired", "rejected"):
                pos["stop_order_id"] = None
                return None
            raise  # stop staat nog open en kon niet weg: niets aanraken, volgende run opnieuw

    def _exit_from_order(self, book, pos, o, reason, learner):
        filled = float(o.get("filled") or pos["qty"])
        avg = float(o.get("average") or o.get("price") or pos.get("exchange_stop_price") or pos["entry_price"])
        cost = float(o.get("cost") or filled * avg)
        fee = (o.get("fee") or {}).get("cost")
        fee = float(fee) if fee is not None else cost * self.fee
        return record_exit(book, pos, avg, cost - fee, fee, self.now, reason, learner, self.cooldown_sec)

    def check_permissions(self):
        """Controleer of de API-sleutel alles mag wat de bot nodig heeft (zonder iets te veranderen)."""
        results = {}
        tests = {
            "Saldo bekijken (Query Funds)": lambda: self.ex.fetch_balance(),
            "Open orders bekijken (Query Open Orders & Trades)": lambda: self.ex.fetch_open_orders(),
            "Oude orders bekijken (Query Closed Orders & Trades)": lambda: self.ex.fetch_closed_orders(None, None, 1),
            "Orders annuleren (Cancel & Close Orders)": lambda: self.ex.cancel_order("OAAAAA-AAAAA-AAAAAA"),
        }
        for name, fn in tests.items():
            try:
                fn()
                results[name] = True
            except Exception as e:
                kind = e.__class__.__name__
                # 'order bestaat niet' betekent dat annuleren wel mag
                results[name] = kind in ("OrderNotFound", "InvalidOrder", "BadRequest") and "ermission" not in str(e)
        return results

    # --- acties ---------------------------------------------------------------------------
    def buy(self, book, market, agent, signal, qty, price, stop, hard, extra, risk_eur, candle_ts, reason, score, plan):
        m = self.ex.market(market)
        amount = float(self.ex.amount_to_precision(market, qty))
        min_amount = (m.get("limits", {}).get("amount") or {}).get("min") or 0
        min_cost = (m.get("limits", {}).get("cost") or {}).get("min") or 0
        if amount < min_amount or amount * price < min_cost:
            # Kleine rekening: afronden naar het Kraken-minimum als dat binnen de grenzen van de risico-agent past
            need = max(min_amount, (min_cost / price) * 1.02 if price else 0)
            bumped = float(self.ex.amount_to_precision(market, need * 1.001))
            limit = (plan or {}).get("max_value", 0)
            if bumped * price * (1 + self.fee) <= min(limit, self.free_eur() * 0.99):
                risk_eur = risk_eur * bumped / max(qty, 1e-12)  # het risico groeit mee met de grotere order
                amount = bumped
                plan["note"] = f"afgerond naar het Kraken-minimum van {min_amount} {m['base']}"
            else:
                return {"ok": False, "text": f"Onder het Kraken-minimum voor {market} (min {min_amount} stuks), en afronden past niet binnen je limieten"}
        o = self.ex.create_order(market, "market", "buy", amount, None, self._params())
        if self.validate:
            test = {"qty": amount, "hard_stop": hard, "stop": stop, "exit_style": signal.exit_style, "market": market}
            try:
                self.ex.create_order(market, "market", "sell", amount, None,
                                     self._params({"stopLossPrice": float(self.ex.price_to_precision(market, hard))}))
                stop_txt = "stop-loss order ook goedgekeurd"
            except Exception as e:
                stop_txt = f"stop-loss order afgekeurd: {e}"
            return {"ok": False, "validated": True,
                    "text": f"VALIDATIEMODUS: Kraken keurde de koop van {amount} {m['base']} goed, niet uitgevoerd; {stop_txt}"}
        try:
            filled, avg, cost, fee_eur, fee_base, _ = self._wait_fill(o["id"], market)
        except Exception as e:
            # De order is wel geplaatst: positie nooit kwijtraken, schatten en volgende runs controleren
            self.errors.append(f"{market}: aankoop geplaatst maar bevestiging ophalen mislukt ({e}); bedragen geschat")
            filled, avg = amount, price * (1 + self.slip)
            cost, fee_eur, fee_base = filled * avg, filled * avg * self.fee, 0.0
        if filled <= 0:
            return {"ok": False, "text": "Order niet (of niet op tijd) uitgevoerd door Kraken"}
        net_qty = filled - fee_base
        fee_total = fee_eur + fee_base * avg
        self._bal = None
        book["cash"] -= cost + fee_eur
        book["fees_paid"] += fee_total
        pos = new_position(book, market, agent, signal, net_qty, avg, cost, fee_eur, stop, hard, extra,
                           risk_eur, candle_ts, self.tf_sec, reason, score, plan)
        pos["entry_cost"] = cost + fee_eur
        try:
            self._place_stop(pos)
            stop_txt = f"stop-loss order op Kraken op {px(pos['exchange_stop_price'])}"
        except Exception as e:
            self.errors.append(f"{market}: stop-loss plaatsen mislukt ({e}), volgende run opnieuw")
            stop_txt = "stop-loss order nog niet geplaatst (volgende run opnieuw)"
        note = f" ({plan['note']})" if plan and plan.get("note") else ""
        return {"ok": True, "text": f"Gekocht: {net_qty:g} {m['base']} tegen gemiddeld {px(avg)}{note}, kosten {eur(fee_total)}; {stop_txt}"}

    def sell(self, book, market, price, ts, reason, learner):
        pos = book["positions"][market]
        if self.validate:
            self.ex.create_order(market, "market", "sell", float(self.ex.amount_to_precision(market, pos["qty"])), None, self._params())
            pos.setdefault("validate_notes", []).append(reason)
            return {"validated": True, "reason": f"VALIDATIEMODUS: verkoop goedgekeurd door Kraken, niet uitgevoerd ({reason})"}
        filled_stop = self._cancel_stop(pos)
        if filled_stop:
            return self._exit_from_order(book, pos, filled_stop, "Stop-loss op Kraken uitgevoerd", learner)
        free = float(self.balance(refresh=True)["free"].get(self._base(market), 0) or 0)
        amount = float(self.ex.amount_to_precision(market, min(pos["qty"], free)))
        if amount <= 0:
            raise RuntimeError(f"geen {self._base(market)} gevonden om te verkopen")
        o = self.ex.create_order(market, "market", "sell", amount, None, self._params())
        try:
            filled, avg, cost, fee_eur, fee_base, _ = self._wait_fill(o["id"], market)
        except Exception as e:
            self.errors.append(f"{market}: verkoop geplaatst maar bevestiging ophalen mislukt ({e}); bedragen geschat")
            filled, avg = amount, price * (1 - self.slip)
            cost, fee_eur, fee_base = filled * avg, filled * avg * self.fee, 0.0
        self._bal = None
        fee = fee_eur + fee_base * avg
        return record_exit(book, pos, avg, cost - fee, fee, ts, reason, learner, self.cooldown_sec)

    def reconcile(self, book, market, last_price, learner):
        """Vergelijk met Kraken: is de stop-loss uitgevoerd? Staat er nog een stop? Is de munt er nog?"""
        pos = book["positions"].get(market)
        if not pos or self.validate:
            return None
        oid = pos.get("stop_order_id")
        if oid:
            try:
                o = self.ex.fetch_order(oid, market)
            except Exception as e:
                self.errors.append(f"{market}: stop-order opvragen mislukt ({e}), controle via saldo")
                o = {"status": "unknown"}
            status = o.get("status")
            if status == "closed":
                return self._exit_from_order(book, pos, o, f"Stop-loss op Kraken uitgevoerd op {px(pos.get('exchange_stop_price') or 0)}", learner)
            if status in ("canceled", "expired", "rejected"):
                pos["stop_order_id"] = None
        total = float(self.balance()["total"].get(self._base(market), 0) or 0)
        if total < pos["qty"] * 0.5:
            if status == "unknown" and pos.get("exchange_stop_price"):
                est = pos["exchange_stop_price"]
                proceeds = pos["qty"] * est
                return record_exit(book, pos, est, proceeds * (1 - self.fee), proceeds * self.fee, self.now,
                                   f"Stop-loss op Kraken uitgevoerd (geschat op {px(est)})", learner, self.cooldown_sec)
            value = total * last_price
            return record_exit(book, pos, last_price, value, 0.0, self.now,
                               "Munt niet meer (volledig) op Kraken gevonden: handmatig verkocht?", learner, self.cooldown_sec)
        return None

    def manage(self, book, market, new_candles, h4_list, learner, last_price):
        pos = book["positions"].get(market)
        if not pos:
            return None
        trade = self.reconcile(book, market, last_price, learner)
        if trade:
            return trade
        hit = advance(pos, new_candles, h4_list, self.exits, self.fee, self.slip,
                      int(pos.get("timeframe_sec") or self.tf_sec), intrabar=False)
        if hit:
            return self.sell(book, market, hit[0], hit[1], hit[2], learner)
        target = float(self.ex.price_to_precision(market, exchange_stop(pos)))
        current = pos.get("exchange_stop_price")
        needs = (not pos.get("stop_order_id") and not self.validate) or current is None \
            or abs(target - current) / current > 0.003
        if needs:
            filled = self._cancel_stop(pos)
            if filled:
                return self._exit_from_order(book, pos, filled, "Stop-loss op Kraken uitgevoerd", learner)
            self._place_stop(pos)
        return None
