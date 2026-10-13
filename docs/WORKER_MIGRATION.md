# Optional persistent Kraken worker — migration checklist

This Docker worker is a **deployable design, NOT a deployed service**.
It does not assume you have a VPS, always-on computer, or safe broker migration.

1. Stop new orders on the old Github Actions trader and disable self-dispatch /
   external triggers, without cancelling protective stop-loss orders.
2. Fetch latest completed state from the OLD runner, then freeze writes.
   Back up `data/state.json` outside the public GitHub repository.
3. On Kraken, reconcile every balance, pending order, filled order and
   protective stop against the ledger. Resolve ambiguous fills manually.
4. Prepare a secure always-on Linux or Docker host. Keep secrets exclusively
   in its environment; rotate credentials if they have ever been exposed.
   **Never copy .env into GitHub.**
5. Start in paper/validation mode; verify health and resilience to restarts.
   A live session additionally requires `KRAKEN_LIVE_CONFIRMED=YES`,
   matching live state, compatible Kraken API permissions and operator oversight.
6. Bind-mount durable data storage with backups. Test automated alert delivery
   for `ENGINE_HALT.json` and stale `ENGINE_HEARTBEAT.json`.
7. Once new state reconciliation is accepted, shut down old live triggers
   permanently before enabling the new worker. Never run two writers to
   the same Kraken account.

The worker uses a single-process filesystem lock (Linux), fsynced atomic
JSON and a sticky error halt. Its "watchdog" here is only Docker's restart
policy; **external alert delivery and disaster recovery remain required**.

To run a paper experiment, use a separate isolated repository copy with
`config.json` mode=`paper`, a separate `data` volume, and no Kraken secrets.
Do not flip the production main branch to paper while managing live orders.

Use the Freqtrade research container separately for comparison; never connect
both Freqtrade and the legacy trader to the same Kraken funds simultaneously.
