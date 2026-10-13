"""Persistent supervised worker, alternative to six-hour GitHub Actions chaining.

Linux process locking is required. This worker never auto-clears the HALT file;
operator must reconcile account and state before restarting a failed live worker.
"""
import datetime
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from agents.storage import save_json

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HALT = DATA / "ENGINE_HALT.json"
HEARTBEAT = DATA / "ENGINE_HEARTBEAT.json"
LOCK = DATA / "ENGINE_LOCK"


def preflight():
    cfg = json.loads((ROOT / "config.json").read_text())
    if HALT.exists():
        raise RuntimeError(f"Persistent safety halt found at {HALT}; operator reconciliation required")
    if cfg["mode"] == "live":
        if not (DATA / "state.json").exists():
            raise RuntimeError("No preserved Kraken ledger; refusing to create a new live account state")
        if (not cfg.get("live", {}).get("validate_only", False)
                and os.environ.get("KRAKEN_LIVE_CONFIRMED") != "YES"):
            raise RuntimeError("Live mode requires KRAKEN_LIVE_CONFIRMED=YES after reconciliation")
        if not os.environ.get("KRAKEN_API_KEY") or not os.environ.get("KRAKEN_API_SECRET"):
            raise RuntimeError("Kraken API credentials missing")
    return cfg


def next_slot():
    now = time.time()
    interval = 300
    return ((int(now) // interval) + 1) * interval + 10


def iteration():
    import main
    state = main.run()
    status = state.get("last_run") or {}
    live = (state.get("books") or {}).get("live") or {}
    heartbeat = {
        "ts": int(time.time()), "ok": bool(status.get("ok")),
        "last_run_ts": status.get("ts"),
        "trading_blocked": bool(live.get("trading_blocked_reason") or live.get("unresolved_order")),
        "live_open_positions": len(live.get("positions") or {}),
    }
    save_json(HEARTBEAT, heartbeat)
    if not heartbeat["ok"] or heartbeat["trading_blocked"]:
        raise RuntimeError("Live health check failed; inspect state/errors and Kraken orders")
    return heartbeat


def run_loop():
    DATA.mkdir(exist_ok=True)
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Kraken worker holds the process lock") from exc
        preflight()
        print("Kraken worker started with process lock", flush=True)
        while True:
            try:
                heartbeat = iteration()
                print("OK", heartbeat, flush=True)
            except BaseException as exc:
                save_json(HALT, {
                    "ts": int(time.time()), "reason": str(exc),
                    "action": "Review Kraken order IDs, balances and stop protection before removing this file",
                })
                print("SAFETY HALT:", exc, file=sys.stderr, flush=True)
                raise
            delay = max(0, next_slot() - time.time())
            time.sleep(delay)


if __name__ == "__main__":
    run_loop()
