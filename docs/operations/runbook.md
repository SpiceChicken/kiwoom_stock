# Operations runbook

## Kill-switch terminal stop

A process exit code of `1` accompanied by `kill_switch` is an abnormal safety stop. It does **not** mean that positions
were liquidated. The engine intentionally creates no actual or paper order and leaves active position objects and ledger
rows unchanged.

When the alert or local critical log appears:

1. Do not automatically restart the same session. A supervisor restart loop could repeatedly reconnect without resolving
   the market exposure that caused the stop.
2. Record the local log timestamp, total PnL, configured loss limit, and immutable unresolved position-code snapshot.
3. Compare every unresolved code with the broker/account view and the OPEN ledger rows. Treat a missing Slack message as
   possible: the engine records whether the notifier callable returned or raised, not whether Slack delivered it.
4. Preserve local output and logs. The kill path deliberately skips the daily reporter, S3 archive, cleanup, and normal
   finish notice, so those artifacts remain available for investigation. It still attempts executor, physical-state
   adapter, queue-worker, and SQLite connection shutdown before exit.
5. Do not mark a position closed or create a liquidation order without a current price, an explicit operator approval,
   and a broker order/acceptance receipt. Those capabilities are outside the current application contract.
6. Start a new runtime only after exposure, ledger consistency, configuration, and the process supervisor's restart policy
   have been reviewed.

The current implementation and temp-SQLite tests prove the in-process queue drain/join/connection-close ordering. They do
not prove actual Slack delivery, real market-hour scheduling, Kiwoom-session shutdown, SIGTERM routing, named-volume
permissions, or worker/DB close during a real container stop. Those real-path checks remain activation gates; local
tests must not be treated as production readiness.

## Normal terminal outcomes

`market_closed` and an engine-caught `user_interrupt` are normal exit-code `0` outcomes. They retain the existing
post-market reporter, archive/cleanup policy, and finish-notification path. Engine persistence is closed before the
post-market report readers open the same configured SQLite path. A `KeyboardInterrupt` outside the engine or during
post-market work retains the process entrypoint's existing immediate-stop behavior.

## Persistence shutdown and recovery

The production runtime owns one `TradeLogger` at `KIWOOM_DB_PATH`. Normal and kill terminal handling follows:

1. latch the engine closed so no new cycle/evaluation starts;
2. wait for accepted evaluation work;
3. close the physical-state submission adapter;
4. append one FIFO sentinel behind accepted tasks, join the drained worker, and close worker/main SQLite connections;
5. only after successful close, enter permitted post-market work.

Repeated close is safe and does not enqueue another sentinel or close completed resources again. Cleanup attempts all
possible later phases after `BaseException`, and one bounded retry pass recovers one-shot failures before reporting the
latched failure. If sentinel publication succeeded before an interruption, a dead worker may leave duplicate control
items; these are removed only when the locked queue inspection proves every remaining item is the identical sentinel. A
physical-state task is never discarded. A persistent incomplete ledger is not marked resource-terminal and a later
explicit engine `close()` retries only that ledger step; executor and physical-adapter steps are not repeated. The main
process still makes one close attempt and enters the crash boundary rather than retrying indefinitely. An ordinary close
failure is not reported as success: normal processing enters the crash/exit-1 boundary, while a kill result remains exit
1 without a duplicate crash Slack attempt. If run and close both fail, the run error remains primary and cleanup failure
is attached as local critical context/note. Process-control exceptions remain process-control exceptions after cleanup.

If persistence shutdown fails:

1. do not start post-market readers or restart automatically;
2. preserve the configured DB, `-wal`/`-shm` companions if present, logs, and output tree without manual edits;
3. record the exact `KIWOOM_DB_PATH`, process/replica count, volume ownership/mode, primary error, and cleanup note;
4. verify there is no second application process and no unexpected cwd `trades.db`;
5. inspect `OPEN` rows read-only and reconcile them with broker/account state before any restart;
6. escalate to an isolated recovery/validator procedure. Do not delete lock/sidecar files or mark rows closed as a
   workaround.

The relative default `trades.db` exists only for legacy direct callers. Managed runs must use an explicit safe path. A
permission or missing-parent error at `/var/lib/kiwoom/trades.db` is a startup failure, not permission to create a
fallback DB elsewhere.
