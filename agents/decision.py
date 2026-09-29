"""Beslis-agent: weegt alle adviezen en zet de koopkansen over alle munten op volgorde.

Per munt en per horizon (kort = 15 minuten, lang = 4 uur):
  score = sterkte beste koopadvies x invloed van die agent (Prestatie-agent)
          + bonus voor elke andere agent met dezelfde horizon die ook 'kopen' zegt
          x marktfase (Markt-agent), tenzij de agent is ingesteld om de marktfase te negeren.
Een 'negatief' advies met sterkte >= veto_strength van een agent met dezelfde horizon blokkeert de aankoop.
Bij een open positie beslist alleen de agent die kocht over verkopen.
"""


class DecisionAgent:
    label = "Beslis-agent"

    def __init__(self, agents, min_score=0.55, veto_strength=0.8, confluence_bonus=0.15):
        self.agents = {a.name: a for a in agents}
        self.min_score, self.veto, self.bonus = min_score, veto_strength, confluence_bonus

    def lab(self, name):
        return self.agents[name].label if name in self.agents else name

    def exit_check(self, signals, position):
        owner = position["agent"]
        s = signals.get(owner)
        if s and s.action == "sell" and s.strength >= 0.5:
            return {"type": "exit", "reason": f"{self.lab(owner)}: {s.reason}"}
        if position.get("exit_style") == "two_stage":
            return {"type": "hold", "reason": "Positie loopt; de tweetraps trailing stop bewaakt de uitstap"}
        return {"type": "hold", "reason": f"Positie aanhouden, {self.lab(owner)} ziet geen reden om te verkopen"}

    def entries(self, signals, weights, regime_mult, regime):
        """Geeft per horizon de beste koopkans (of de reden waarom niet)."""
        out = []
        for horizon in ("lang", "kort"):
            group = {n: s for n, s in signals.items() if self.agents[n].horizon == horizon}
            buys = [(n, s) for n, s in group.items() if s.action == "buy"]
            if not buys:
                continue
            vetoes = [(n, s) for n, s in group.items() if s.action == "sell" and s.strength >= self.veto]
            if vetoes:
                n, s = vetoes[0]
                out.append({"type": "hold", "score": 0.0, "horizon": horizon,
                            "reason": f"Koopadvies geblokkeerd door {self.lab(n)}: {s.reason}"})
                continue
            name, sig = max(buys, key=lambda x: x[1].strength * weights[x[0]])
            mult = regime_mult if self.agents[name].use_regime else 1.0
            score = (sig.strength * weights[name] + self.bonus * (len(buys) - 1)) * mult
            backers = ", ".join(self.lab(n) for n, _ in buys)
            calc = (f"sterkte {sig.strength:.2f} x invloed {weights[name]:.2f}"
                    + (f" + {len(buys) - 1}x bonus" if len(buys) > 1 else "")
                    + (f" x markt {mult:.1f}" if self.agents[name].use_regime else " (marktfase genegeerd)")
                    + f" = score {score:.2f}")
            if mult == 0:
                out.append({"type": "hold", "score": 0.0, "horizon": horizon,
                            "reason": f"{self.lab(name)} wil kopen, maar de markt is {regime}: geen nieuwe aankopen"})
            elif score < self.min_score:
                out.append({"type": "hold", "score": score, "horizon": horizon,
                            "reason": f"Koopkans te zwak ({calc}, minimaal {self.min_score})"})
            else:
                out.append({"type": "entry", "agent": name, "signal": sig, "score": score, "horizon": horizon,
                            "reason": f"{self.lab(name)}: {sig.reason}", "calc": f"{calc}; eens: {backers}"})
        return out
