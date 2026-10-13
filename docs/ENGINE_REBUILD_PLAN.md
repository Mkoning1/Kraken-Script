# Kraken engine — audit and rebuild plan (2026-10-13)

## Decision

Do **not** replace the current Kraken live trader with an untested new bot. Keep the
current execution path on `main` unchanged until the safety branch passes CI and
has been reviewed. No dashboard redesign in this phase.

### Facts from repository state at 2026-10-13 06:58 UTC

- 2 completed live trades, 23 completed paper trades; 0 open live positions.
- Six strategy agents, but only `trend4h` is live-eligible; five are shadow-only.
- 2,016 recorded scans in the rolling seven-day log. Signal generation is not fully broken.
- The 1h historical simulator intentionally skips 15m strategies; they have no
  comparable validated out-of-sample backtest.
- The 2026-10-11 backtest: `trend4h` OOS +0.193 R, but OOS without best market
  -0.067 R; `mean_reversion` OOS -0.472 R; `breakout` OOS -0.153 R.
- Current `assertief` profile allows 100% of equity in one position, while README
  describes 45%; live free-EUR budget is set to `alles`.
- The workflow uses a 340-minute GitHub-hosted runner as a quasi-continuous server.
  It depends on repository state commits and workflow self-dispatch for continuity.
- The repository is public and publishes transaction/state snapshots. Do not store
  secrets in tracked files; restrict telemetry before publishing any replacement UI.

## Non-negotiable gates

1. Exchange-side protection: any newly bought live position must have a confirmed
   Kraken stop order ID. If protection cannot be confirmed, retain the position
   for reconciliation, block all additional live purchases and surface a critical alert.
   This blocks additional exposure; it **does not** retroactively guarantee a stop.
2. No error should cause the worker to keep trading while unable to pull/push state.
3. No order after unknown/missing live account state; reconcile against Kraken.
4. No auto-promotion on in-sample results or a concentrated OOS result; 15m agents
   stay shadow-only until validated on 15m historical data.
5. Use conservative assumptions for fee and slippage; paper results never imply profit.
6. Do not automatically bypass protective safeguards to increase trade frequency.

## Engine choice

**Now:** Preserve ccxt/Kraken execution while hardened and measured. Switching
execution engines would create migration risk for small positions, order IDs,
pending exchange stops and account state.

**Research benchmark:** Freqtrade is the first comparison target. It has native
Kraken spot support, on-exchange stops, documented dry-run/backtesting, and more
reliable mature order-state modeling than a custom script. Jesse may be compared
for strategy research; Hummingbot mainly for market-making, not a direct substitute.

**Infrastructure target:** a persistent, supervised worker with transactional
local storage and alerting rather than a six-hour GitHub Actions job. A short-lived
CI build must never carry the only copy of execution state. This target requires
a separate migration and operational acceptance test. Avoid moving state while
live orders are open.

**Research agent:** read-only daily analysis of missed entries, effective trading
costs, OOS/rolling walk-forward evaluation, concentration, and benchmark results.
It may propose code changes via PR but never alter trading thresholds or submit
live orders on its own.

## Next validation work before deployment

- Run `python -m unittest discover -s tests -p 'test_*.py' -v`,
  `python -m tests.quick`, and `python scripts/audit_engine.py --json`.
- Simulate Kraken stop-order rejection, intermittent network response, partial fill,
  missed GitHub commits and restart against known balances. Current branch only
  covers the stop rejection / fail-closed path; ambiguous fills still need
  reconciliation tests before claiming full safety.
- Build true 15m historical OOS tests with correct candle alignment and timestamps.
  Do not resample hourly candles to 15m. Kraken REST limits historical OHLCV
  retrieval; obtain lawful archived 15m data or record it prospectively.
- Compare BTC buy-and-hold and a fees-aware baseline on identical periods.
- Check exchange rate limits, Kraken key scopes, stop-order semantics,
  transaction integrity, GitHub Actions costs, and operating alerts.
- Explicitly review `assertief` max-position and full-account budget before merging.
- Make the repository private or keep state outside public Git; remove already
  published private financial details from history separately, if required.
- Keep the existing dashboard unchanged until execution and data contracts stabilize.

## Operational limitation

A draft PR is **not deployed**. Never treat CI and mock backtests alone as approval
to release a live crypto trader. Do a supervised dry run and small-limit reconciliation
before going live, and keep exchange-side stop orders checked on Kraken.
