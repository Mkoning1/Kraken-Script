"""Prestatie-agent: meet per strategie-agent het resultaat, bepaalt zijn invloed en of hij met echt geld mag handelen.

Resultaat in R: winst of verlies gedeeld door het risico bij instap (+1R = evenveel gewonnen als geriskeerd).
Invloed = 1 + gemiddelde R over de laatste trades (begrensd), pas na min_trades trades.

Echt geld of schaduw:
  - Een agent in de schaduw handelt met nepgeld.
  - Met een goede backtest (minstens backtest_min_r per trade over de testperiode die niet gebruikt is om
    te optimaliseren): snelle promotie na fast_track_shadow_trades schaduwtrades met minstens fast_track_shadow_r.
  - Met een negatieve backtest: geen promotie.
  - Zonder (genoeg) backtest: promotie na promote_after_trades trades met minstens promote_expectancy_r.
  - Een agent met echt geld die over de laatste demote_window trades gemiddeld onder
    demote_expectancy_r zit, gaat terug naar de schaduw.
  - 'force' in config.json overschrijft dit altijd.
"""


class LearningAgent:
    label = "Prestatie-agent"

    def __init__(self, state, learning, promotion, agent_cfgs, backtest=None, bt_min_trades=30):
        self.backtest = backtest or {}
        self.bt_min_trades = bt_min_trades
        self.stats = state.setdefault("agent_stats", {})
        self.status_map = state.setdefault("agent_status", {})
        self.timeframe_map = state.setdefault("agent_timeframes", {})
        self.l, self.p, self.acfg = learning, promotion, agent_cfgs
        for name, c in agent_cfgs.items():
            tf = int(c.get("timeframe_minutes", 240 if name == "trend4h" else 60))
            previous_tf = self.timeframe_map.get(name)
            if previous_tf is not None and int(previous_tf) != tf:
                # Een strategie op een ander timeframe is statistisch een nieuwe strategie.
                # Oude R/trades mogen dus niet zorgen voor een onterechte promotie.
                self.stats.pop(name, None)
                self.status_map[name] = "live" if (promotion.get("all_agents_live") or c.get("start") == "live") else "schaduw"
            self.timeframe_map[name] = tf
            if name not in self.status_map:
                self.status_map[name] = "live" if (promotion.get("all_agents_live") or c.get("start") == "live") else "schaduw"

    def _h(self, agent):
        return self.stats.setdefault(agent, {"r": [], "pnl_live": 0.0, "pnl_schaduw": 0.0,
                                             "trades": 0, "wins": 0, "live_since": None})

    def record(self, trade):
        h = self._h(trade["agent"])
        h["r"] = (h["r"] + [{"r": round(trade["r"], 3), "book": trade["book"]}])[-100:]
        h[f"pnl_{trade['book']}"] = h.get(f"pnl_{trade['book']}", 0.0) + trade["pnl"]
        h["trades"] += 1
        h["wins"] += trade["pnl"] > 0

    def weight(self, agent):
        rs = [x["r"] for x in self._h(agent)["r"]]
        if not self.l["enabled"] or len(rs) < self.l["min_trades"]:
            return 1.0
        recent = rs[-self.l["window"]:]
        return max(self.l["min_weight"], min(self.l["max_weight"], 1 + sum(recent) / len(recent)))

    def status(self, agent):
        forced = (self.acfg.get(agent) or {}).get("force")
        if forced in ("live", "schaduw"):
            return forced
        if self.p.get("all_agents_live"):
            return "live"
        return self.status_map.get(agent, "schaduw")

    def review(self, labels):
        """Promoveer of degradeer agents. Geeft meldingen terug voor het logboek."""
        notes = []
        if not self.p.get("enabled"):
            return notes
        for agent, cur in list(self.status_map.items()):
            if (self.acfg.get(agent) or {}).get("force"):
                continue
            rs = [x["r"] for x in self._h(agent)["r"]]
            if cur == "schaduw":
                bt = self.bt_verdict(agent)
                if bt == "negatief":
                    continue
                need, min_r = ((self.p["fast_track_shadow_trades"], self.p["fast_track_shadow_r"]) if bt == "positief"
                               else (self.p["promote_after_trades"], self.p["promote_expectancy_r"]))
                if len(rs) >= need:
                    recent = rs[-max(need, 20):]
                    exp = sum(recent) / len(recent)
                    if exp >= min_r:
                        self.status_map[agent] = "live"
                        notes.append(f"{labels.get(agent, agent)} gepromoveerd naar echt geld: gemiddeld {exp:+.2f}R over {len(recent)} schaduwtrades"
                                     + (", backtest positief" if bt == "positief" else ""))
            elif cur == "live":
                live_rs = [x["r"] for x in self._h(agent)["r"] if x["book"] == "live"]
                w = self.p["demote_window"]
                if len(live_rs) >= w:
                    exp = sum(live_rs[-w:]) / w
                    if exp <= self.p["demote_expectancy_r"]:
                        self.status_map[agent] = "schaduw"
                        notes.append(f"{labels.get(agent, agent)} terug naar schaduw: gemiddeld {exp:+.2f}R over de laatste {w} echte trades")
        return notes

    def bt_verdict(self, agent):
        b = self.backtest.get(agent)
        if not b:
            return "geen"
        cfg_tf = int((self.acfg.get(agent) or {}).get("timeframe_minutes", 240 if agent == "trend4h" else 60))
        bt_tf = int(b.get("timeframe_minutes", 240 if agent == "trend4h" else 60))
        if cfg_tf != bt_tf:
            return "geen"
        trades, exp = b.get("oos_trades", 0), b.get("oos_expectancy_r")
        if exp is None or trades < max(10, self.bt_min_trades // 3):
            trades, exp = b.get("trades", 0), b.get("expectancy_r")
        if exp is None or trades < max(10, self.bt_min_trades // 3):
            return "geen"
        if exp < 0:
            return "negatief"
        ex_best = b.get("ex_best_expectancy_r")
        if ex_best is not None and ex_best < 0:
            return "neutraal"  # winst hangt aan één munt: geen snelle route naar echt geld
        return "positief" if exp >= self.p.get("backtest_min_r", 0.1) else "neutraal"

    def summary(self, agents):
        out = []
        for a in agents:
            h = self._h(a.name)
            rs = [x["r"] for x in h["r"]]
            recent = rs[-self.l["window"]:]
            bt = self.bt_verdict(a.name)
            need_total = self.p["fast_track_shadow_trades"] if bt == "positief" else self.p["promote_after_trades"]
            need = max(0, need_total - len(rs))
            out.append({
                "name": a.name, "agent": a.label, "horizon": a.horizon, "status": self.status(a.name),
                "weight": round(self.weight(a.name), 2), "trades": h["trades"],
                "win_rate_pct": round(h["wins"] / h["trades"] * 100) if h["trades"] else None,
                "pnl_live": round(h.get("pnl_live", 0.0), 2), "pnl_schaduw": round(h.get("pnl_schaduw", 0.0), 2),
                "expectancy_r": round(sum(recent) / len(recent), 2) if recent else None,
                "promotion_note": ("backtest negatief: blijft in de schaduw" if bt == "negatief" else
                                   f"nog {need} schaduwtrades nodig" + (" (snelle route, backtest positief)" if bt == "positief" else "") if need else
                                   "wacht op een beter gemiddelde")
                                  if self.status(a.name) == "schaduw" else None,
                "backtest": self.backtest.get(a.name) if self.bt_verdict(a.name) != "geen" else None,
            })
        return out
