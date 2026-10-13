"""Read-only health and strategy audit. Does not connect to Kraken or place orders.

Usage:
    python scripts/audit_engine.py
    python scripts/audit_engine.py --json
"""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    p = ROOT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def audit(now=None):
    now = int(now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp())
    cfg, state, bt = load("config.json"), load("data/state.json"), load("data/backtest.json")
    if not cfg or not state:
        raise RuntimeError("config.json and data/state.json are required")
    profile = cfg.get("profiles", {}).get(cfg.get("profile"), {})
    live = (state.get("books") or {}).get("live") or {}
    paper = (state.get("books") or {}).get("schaduw") or {}
    run = state.get("last_run") or {}
    ts = int(run.get("ts") or 0)
    fresh = bool(ts and 0 <= now - ts <= 1200)
    stopped = bool(live.get("halted") or live.get("trading_blocked_reason"))
    unprotected = [
        market for market, pos in (live.get("positions") or {}).items()
        if not pos.get("stop_order_id") or pos.get("protection_pending")
    ]
    recent = [e for e in state.get("events", []) if e.get("ts", 0) >= now - 7 * 86400]
    reasons = collections.Counter(
        ("daily_limit" if "Daglimiet" in e.get("reason", "") else
         "cooldown" if "Afkoelperiode" in e.get("reason", "") else
         "other")
        for e in recent if e.get("type") == "blocked"
    )
    strategy = {}
    agent_backtests = bt.get("agents") or {}
    for name, opts in (cfg.get("agents") or {}).items():
        s = (state.get("agent_stats") or {}).get(name, {})
        report = agent_backtests.get(name) or {}
        tf = int(opts.get("timeframe_minutes", 60))
        same_timeframe = tf == int(report.get("timeframe_minutes", -1))
        out = report.get("oos_expectancy_r") if same_timeframe else None
        robust = report.get("oos_ex_best_expectancy_r") if same_timeframe else None
        strategy[name] = {
            "timeframe_minutes": tf,
            "status": (state.get("agent_status") or {}).get(name, opts.get("start")),
            "closed_trades_observed": s.get("trades", 0),
            "backtest_oos_trades": report.get("oos_trades") if same_timeframe else None,
            "oos_expectancy_r": out,
            "oos_ex_best_expectancy_r": robust,
            "has_robust_positive_test": out is not None and robust is not None and out > 0 and robust > 0,
        }
    flags = []
    if not fresh:
        flags.append("STALE_WORKER: last successful heartbeat is older than 20 minutes or missing")
    if not run.get("ok"):
        flags.append("LAST_RUN_FAILED")
    if stopped:
        flags.append("LIVE_HALTED_OR_BLOCKED")
    if unprotected:
        flags.append("UNCONFIRMED_EXCHANGE_STOPS: " + ", ".join(unprotected))
    if cfg.get("live", {}).get("budget_eur") == "alles":
        flags.append("FULL_ACCOUNT_BUDGET: all free EUR is eligible")
    if profile.get("max_position_pct", 0) > 50:
        flags.append("HIGH_SINGLE_POSITION_CAP: greater than 50%")
    if not agent_backtests:
        flags.append("NO_BACKTEST_REPORT")
    else:
        bt_time = int(bt.get("generated_ts") or 0)
        if now - bt_time > 8 * 86400:
            flags.append("BACKTEST_STALE: older than 8 days")
        if any(
            info["status"] == "live" and not info["has_robust_positive_test"]
            for info in strategy.values()
        ):
            flags.append("LIVE_STRATEGY_WITHOUT_ROBUST_OUT_OF_SAMPLE_RESULT")
    return {
        "as_of_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        "last_run_utc": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None,
        "last_run_ok": run.get("ok"), "engine_fresh": fresh,
        "live_trading_config_enabled": cfg.get("live", {}).get("trading_enabled"),
        "live_account_eur_budget": cfg.get("live", {}).get("budget_eur"),
        "live_closed_trades": len(live.get("trades") or []),
        "paper_closed_trades": len(paper.get("trades") or []),
        "live_open_markets": list((live.get("positions") or {}).keys()),
        "paper_open_markets": list((paper.get("positions") or {}).keys()),
        "live_block_reason": live.get("trading_blocked_reason"),
        "recent_rejection_reasons": dict(reasons),
        "strategies": strategy, "flags": flags,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("KRAKEN ENGINE AUDIT — " + report["as_of_utc"])
        print("Last run:", report["last_run_utc"], "healthy:", report["engine_fresh"])
        print("Live trades:", report["live_closed_trades"], "paper trades:", report["paper_closed_trades"])
        print("Open live:", report["live_open_markets"], "open paper:", report["paper_open_markets"])
        print("Rejections (7d):", report["recent_rejection_reasons"])
        for name, s in report["strategies"].items():
            print(" ", name, s["status"], "OOS", s["oos_expectancy_r"],
                  "robust", s["oos_ex_best_expectancy_r"],
                  "validated", s["has_robust_positive_test"])
        print("Flags:", "; ".join(report["flags"]) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
