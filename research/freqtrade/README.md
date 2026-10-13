# Freqtrade Kraken dry-run benchmark (strictly research-only)

This separate environment is NOT the live Kraken bot and does NOT migrate, import
or touch active Kraken orders. It starts **stopped**, is **dry_run=true**,
holds **no API credentials** and uses a simulated EUR wallet.

Docs: https://www.freqtrade.io/en/stable/docker_quickstart/
Kraken support: https://docs.freqtrade.io/en/latest/exchanges/

## Running the benchmark (requires Docker)

```bash
cd research/freqtrade
docker compose pull
docker compose up -d
docker compose logs -f
```

To explicitly start simulated trading inside Freqtrade when ready to observe:

```bash
docker compose exec freqtrade-dry freqtrade --help
```

The config's `initial_state` is stopped: change it to `running` ONLY in
this dry-run profile once ready for virtual orders; no real API credentials are
ever required.

For historical Kraken datasets: Kraken REST OHLCV is limited to 720 candles.
Use archived **trade** downloads or another independently validated historical
source. Do not claim that 720 bars represent two years of testing.

## How we compare

- Same historical dates, EUR pair universe and fees, and a hold-BTC baseline.
- Match signal timestamp and next executable candle, stop-order semantics,
  volatility regime, drawdown, slippage and trade count.
- `TrendBreakoutResearch` is an **exploratory control**. It is *not*
  a one-to-one port of custom Trend-4u's ATR exit logic, so returns cannot be
  directly ranked as a strategy winner until that difference is removed.
- No auto-promotion to real Kraken orders, ever, from this research environment.
- A separate always-on deployment requires durable secrets, encrypted storage,
  full Kraken-state reconciliation, supervised paper trading, alerting and
  explicit authorization; Docker dry-run does not establish that.
