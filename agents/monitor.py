"""Monitor-agent: houdt het logboek bij, schrijft per run een verslag in gewone taal en maakt de dashboard-data.
Houdt de bestanden klein, want ze worden elke run in GitHub opgeslagen."""
import json
from pathlib import Path

from .fmt import eur

RECENT_POINTS = 4 * 24 * 3
OLD_POINTS = 24 * 120
MAX_EVENTS = 400


def book_equity(book, prices):
    return book["cash"] + sum(p["qty"] * prices.get(m, p["entry_price"]) for m, p in book["positions"].items())


def book_exposure(book, prices):
    return sum(p["qty"] * prices.get(m, p["entry_price"]) for m, p in book["positions"].items())


def _compact(history):
    if len(history) <= RECENT_POINTS:
        return history
    old, recent = history[:-RECENT_POINTS], history[-RECENT_POINTS:]
    hourly, seen = [], set()
    for pt in reversed(old):
        h = pt["ts"] // 3600
        if h not in seen:
            seen.add(h)
            hourly.append(pt)
    return list(reversed(hourly))[-OLD_POINTS:] + recent


def book_stats(book, eq, bench_ratio):
    trades = book["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    peak = max_dd = 0
    for pt in book["equity_history"]:
        peak = max(peak, pt["equity"])
        max_dd = max(max_dd, (peak - pt["equity"]) / peak * 100 if peak else 0)
    start = book["starting_capital"] or 1
    return {
        "equity": round(eq, 2), "start": round(book["starting_capital"], 2),
        "return_pct": round((eq / start - 1) * 100, 2),
        "benchmark_return_pct": round((bench_ratio - 1) * 100, 2) if bench_ratio else None,
        "trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "profit_factor": round(gw / gl, 2) if gl else None,
        "realized_pnl": round(sum(t["pnl"] for t in trades), 2),
        "fees_paid": round(book["fees_paid"], 2), "max_drawdown_pct": round(max_dd, 2),
    }


class MonitorAgent:
    label = "Monitor-agent"

    def __init__(self, state_path, dashboard_path):
        self.state_path = Path(state_path)
        self.dashboard_path = Path(dashboard_path)

    def record(self, state, run_ts, prices, bench_price):
        for book in state["books"].values():
            if not book:
                continue
            eq = book_equity(book, prices)
            book.setdefault("bench_start", bench_price)
            ratio = bench_price / book["bench_start"] if bench_price and book.get("bench_start") else None
            book["equity_history"].append({"ts": run_ts, "equity": round(eq, 2),
                                           "benchmark": round(book["starting_capital"] * ratio, 2) if ratio else None})
            book["equity_history"] = _compact(book["equity_history"])
            book["trades"] = book["trades"][-1000:]
        state["events"] = state["events"][-MAX_EVENTS:]
        state["errors"] = state["errors"][-30:]

    @staticmethod
    def narrative(state, cfg, regime, n_markets, events, new_4h, errors, validate):
        """Verslag van deze run in gewone taal."""
        lines = []
        live = state["books"].get("live")
        if cfg["mode"] == "live" and validate:
            lines.append("Validatiemodus: alles draait met je echte saldo, maar Kraken keurt orders alleen goed zonder ze uit te voeren.")
        for name, ok in (state.get("permissions") or {}).items():
            if not ok:
                lines.append(f"LET OP: je API-sleutel mist het recht '{name}'. Zet dit aan op Kraken.")
        lines.append(f"{n_markets} munten bekeken. Markt: {regime['label']}. {regime['reason']}.")
        lines.append("Er is een nieuwe 4-uurscandle gesloten, dus de Trend-4u-agent heeft de munten opnieuw beoordeeld."
                     if new_4h else "Multi-speed actief: bewaking elke 5 min, fast desk elke 15 min, swing desk ieder uur en Trend-4u op gesloten 4-uurscandles.")
        for e in events:
            where = "echt geld" if e.get("book") == "live" else "schaduw"
            if e["type"] == "entry":
                lines.append(f"Gekocht ({where}): {e['market']}. {e['reason']}.")
            elif e["type"] == "exit":
                lines.append(f"Verkocht ({where}): {e['market']}, resultaat {eur(e.get('pnl', 0))}. {e['reason']}.")
            elif e["type"] in ("blocked", "validated", "note"):
                lines.append(f"{e['market'] + ': ' if e.get('market') else ''}{e['reason']}")
        if not any(e["type"] in ("entry", "exit") for e in events):
            lines.append("Geen aankopen of verkopen deze ronde.")
        if live:
            lines.append(f"Echt geld: {len(live['positions'])} open positie(s).")
        if errors:
            lines.append(f"{len(errors)} melding(en), zie onderaan.")
        return lines

    def save(self, state, cfg, profile, prices, agent_summary, extra):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False))

        books = {}
        for name, book in state["books"].items():
            if not book:
                continue
            eq = book_equity(book, prices)
            ratio = (prices.get(cfg["benchmark_market"]) / book["bench_start"]) if book.get("bench_start") and prices.get(cfg["benchmark_market"]) else None
            positions = []
            for m, p in book["positions"].items():
                price = prices.get(m, p["entry_price"])
                value = p["qty"] * price
                positions.append({
                    "market": m, "agent": p["agent"], "qty": p["qty"], "entry_price": p["entry_price"], "entry_ts": p["entry_ts"],
                    "entry_cost": round(p["entry_cost"], 2), "price": price, "value": round(value, 2),
                    "unrealized": round(value - p["entry_cost"], 2), "stop": p["stop"], "hard_stop": p.get("hard_stop"),
                    "exit_style": p["exit_style"], "is_runner": p.get("is_runner"), "peak": p.get("peak"),
                    "stop_on_exchange": bool(p.get("stop_order_id")), "exchange_stop_price": p.get("exchange_stop_price"),
                    "entry_reason": p["entry_reason"], "plan": p.get("plan"),
                })
            books[name] = {
                "stats": book_stats(book, eq, ratio), "cash": round(book["cash"], 2),
                "exposure": round(book_exposure(book, prices), 2), "risk": book.get("risk_status"),
                "positions": positions, "trades": book["trades"][-60:][::-1],
                "equity_history": book["equity_history"],
            }
        dashboard = {
            "generated_ts": state["last_run"]["ts"], "mode": cfg["mode"],
            "validate_only": cfg["live"]["validate_only"], "budget_eur": cfg["live"]["budget_eur"],
            "started_ts": state["started_ts"], "profile": cfg["profile"], "risk_limits": profile,
            "fee_pct": cfg["fee_pct"], "benchmark_market": cfg["benchmark_market"],
            "regime": state["last_run"]["regime"], "books": books, "agents": agent_summary,
            "evaluations": state["evaluations"], "events": state["events"][-50:][::-1],
            "narrative": state["narrative"], "errors": state["errors"][-10:][::-1],
            "last_run_ok": state["last_run"]["ok"], "legacy_trades": state.get("legacy_trades", [])[::-1],
            "levels": {m: v for m, v in state.get("levels", {}).items() if m in extra.get("markets", [])},
            **extra,
        }
        self.dashboard_path.parent.mkdir(parents=True, exist_ok=True)
        self.dashboard_path.write_text(json.dumps(dashboard, separators=(",", ":"), ensure_ascii=False))
