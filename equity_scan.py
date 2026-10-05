"""Standalone China/HK equity scout.

Runs separately from the live Kraken trader. It writes docs/equities.json and a paper-only
equity state, so a data-provider problem here can never block crypto execution.
"""
import json
import time
from pathlib import Path

from agents.equities import EquityScout, build_opportunity_board, paper_dashboard, update_paper_book

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"
STATE = ROOT / "data" / "equity_state.json"
CACHE = ROOT / "data" / "equity_fundamentals.json"
OUT = ROOT / "docs" / "equities.json"
CRYPTO = ROOT / "docs" / "data.json"


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def run(now_ts=None, provider=None):
    now_ts = int(now_ts or time.time())
    cfg = load_json(CONFIG, {}).get("equities", {})
    previous = load_json(OUT, {})
    if not cfg.get("enabled", False):
        result = {"generated_ts": now_ts, "enabled": False, "paper_only": True, "candidates": []}
        save_json(OUT, result)
        return result

    state = load_json(STATE, {})
    try:
        scout = EquityScout(cfg, CACHE, provider=provider)
        report = scout.scan(now_ts)
        book = update_paper_book(state, report, cfg, now_ts)
        state["last_scan_ts"] = now_ts
        state["report"] = report
        save_json(STATE, state)

        crypto = load_json(CRYPTO, {})
        board = build_opportunity_board(crypto.get("scanner"), report, limit=12)
        result = {
            **report,
            "paper": paper_dashboard(book, report),
            "opportunity_board": board,
            "broker": {
                "provider": "Interactive Brokers",
                "live_enabled": False,
                "status": "not_configured",
                "reason": "Equity desk is paper-only until an authenticated broker session and trading permissions are configured.",
            },
            "error": None,
        }
        save_json(OUT, result)
        return result
    except Exception as exc:
        # Preserve the last good research snapshot; only annotate the failure.
        fallback = dict(previous) if previous else {
            "enabled": True, "paper_only": True, "market": cfg.get("market"), "candidates": [],
            "paper": paper_dashboard(state.get("equity_paper", {
                "currency": "HKD", "starting_capital": cfg.get("paper_capital_hkd", 10000),
                "cash": cfg.get("paper_capital_hkd", 10000), "positions": {}, "trades": [],
                "equity_history": [], "fees_paid": 0,
            }), {"candidates": []}),
        }
        fallback["last_attempt_ts"] = now_ts
        fallback["error"] = str(exc)
        save_json(OUT, fallback)
        return fallback


if __name__ == "__main__":
    result = run()
    print(f"Equity scout: {len(result.get('candidates', []))} kandidaten; "
          f"paper equity {result.get('paper', {}).get('equity', 'n/a')} HKD")
    if result.get("error"):
        print("LET OP:", result["error"])
