"""Prestatie-agent: meet per strategie-agent het resultaat, bepaalt zijn invloed en of hij met echt geld mag handelen.

Resultaat in R: winst of verlies gedeeld door het risico bij instap (+1R = evenveel gewonnen als geriskeerd).
Invloed = 1 + gemiddelde R over de laatste trades (begrensd), pas na min_trades trades.

Echt geld of schaduw:
  - Een agent in de schaduw handelt met nepgeld. Na promote_after_trades trades met gemiddeld
    minstens promote_expectancy_r per trade (na kosten) krijgt hij echt geld.
  - Een agent met echt geld die over de laatste demote_window trades gemiddeld onder
    demote_expectancy_r zit, gaat terug naar de schaduw.
  - 'force' in config.json overschrijft dit altijd.
"""


class LearningAgent:
    label = "Prestatie-agent"

    def __init__(self, state, learning, promotion, agent_cfgs):
        self.stats = state.setdefault("agent_stats", {})
        self.status_map = state.setdefault("agent_status", {})
        self.l, self.p, self.acfg = learning, promotion, agent_cfgs
        for name, c in agent_cfgs.items():
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
            if cur == "schaduw" and len(rs) >= self.p["promote_after_trades"]:
                recent = rs[-20:]
                exp = sum(recent) / len(recent)
                if exp >= self.p["promote_expectancy_r"]:
                    self.status_map[agent] = "live"
                    notes.append(f"{labels.get(agent, agent)} gepromoveerd naar echt geld: gemiddeld {exp:+.2f}R over {len(recent)} trades")
            elif cur == "live":
                live_rs = [x["r"] for x in self._h(agent)["r"] if x["book"] == "live"]
                w = self.p["demote_window"]
                if len(live_rs) >= w:
                    exp = sum(live_rs[-w:]) / w
                    if exp <= self.p["demote_expectancy_r"]:
                        self.status_map[agent] = "schaduw"
                        notes.append(f"{labels.get(agent, agent)} terug naar schaduw: gemiddeld {exp:+.2f}R over de laatste {w} echte trades")
        return notes

    def summary(self, agents):
        out = []
        for a in agents:
            h = self._h(a.name)
            rs = [x["r"] for x in h["r"]]
            recent = rs[-self.l["window"]:]
            need = max(0, self.p["promote_after_trades"] - len(rs))
            out.append({
                "name": a.name, "agent": a.label, "horizon": a.horizon, "status": self.status(a.name),
                "weight": round(self.weight(a.name), 2), "trades": h["trades"],
                "win_rate_pct": round(h["wins"] / h["trades"] * 100) if h["trades"] else None,
                "pnl_live": round(h.get("pnl_live", 0.0), 2), "pnl_schaduw": round(h.get("pnl_schaduw", 0.0), 2),
                "expectancy_r": round(sum(recent) / len(recent), 2) if recent else None,
                "promotion_note": (f"nog {need} trades nodig" if need else
                                   f"heeft gemiddeld {self.p['promote_expectancy_r']:+.2f}R nodig")
                                  if self.status(a.name) == "schaduw" else None,
            })
        return out
